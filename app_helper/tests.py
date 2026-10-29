from datetime import timedelta
from unittest.mock import patch

from django.core.cache import cache
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from app_event.models import (
    EventLog, ExternalCallMetric, RequestMetric, SystemMetric,
)
from app_helper import (
    external_http, load_tracker, metrics, middleware, service_helper,
)


class LoadStatsMessageTests(SimpleTestCase):
    """Сводка нагрузки для health/status API: пороги и сообщение."""

    @staticmethod
    def _stats(**overrides):
        stats = {
            'minutes': 60,
            'start': None,
            'end': None,
            'total': 0,
            'user': 0,
            'bot': 0,
            'monitoring': 0,
            'errors': 0,
            'error_rate': 0.0,
            'p95_ms': 0,
            'avg_ms': 0,
            'max_ms': 0,
            'avg_db_ms': 0,
            'avg_db_queries': 0,
            'slow_count': 0,
            'peak_per_minute': 0,
            'requests_per_minute': 0,
            'has_data': True,
            'source': 'db',
        }
        stats.update(overrides)
        return stats

    def test_high_load_by_request_count(self):
        high = metrics.HIGH_LOAD_THRESHOLD + 5_000
        stats = self._stats(
            user=high, peak_per_minute=250, requests_per_minute=250,
        )
        with patch.object(metrics, 'get_window_stats', return_value=stats):
            result = metrics.get_load_stats()

        self.assertTrue(result['is_high_load'])
        self.assertIn(str(high), result['message'])
        self.assertIn(str(metrics.HIGH_LOAD_THRESHOLD), result['message'])

    def test_high_load_by_p95(self):
        stats = self._stats(user=100, p95_ms=5000)
        with patch.object(metrics, 'get_window_stats', return_value=stats):
            result = metrics.get_load_stats()

        self.assertTrue(result['is_high_load'])
        self.assertIn('p95', result['message'])

    def test_high_load_by_error_rate(self):
        stats = self._stats(user=100, errors=10, error_rate=0.1)
        with patch.object(metrics, 'get_window_stats', return_value=stats):
            result = metrics.get_load_stats()

        self.assertTrue(result['is_high_load'])
        self.assertIn('ошибок', result['message'])

    def test_single_error_does_not_trigger_error_rate(self):
        # 1 ошибка из 10 запросов — ниже минимума для оценки доли ошибок.
        stats = self._stats(user=10, errors=1, error_rate=0.1)
        with patch.object(metrics, 'get_window_stats', return_value=stats):
            result = metrics.get_load_stats()

        self.assertFalse(result['is_high_load'])

    def test_normal_load_message_has_numbers(self):
        stats = self._stats(user=100, p95_ms=50)
        with patch.object(metrics, 'get_window_stats', return_value=stats):
            result = metrics.get_load_stats()

        self.assertFalse(result['is_high_load'])
        self.assertIn('100', result['message'])
        self.assertIn('50', result['message'])

    def test_no_data_is_reported_honestly(self):
        stats = self._stats(has_data=False)
        with patch.object(metrics, 'get_window_stats', return_value=stats):
            result = metrics.get_load_stats()

        self.assertFalse(result['has_data'])
        self.assertFalse(result['is_high_load'])
        self.assertIn('нет данных', result['message'])
        self.assertNotIn('Нагрузка в норме', result['message'])


class LoadTrackerBufferTests(SimpleTestCase):
    """Кольцевой буфер: классы трафика и пик (live-слой)."""

    def setUp(self):
        load_tracker.reset()

    def tearDown(self):
        load_tracker.reset()

    def test_client_load_excludes_monitoring(self):
        for _ in range(3):
            load_tracker.record_request(load_tracker.TRAFFIC_USER)
        for _ in range(2):
            load_tracker.record_request(load_tracker.TRAFFIC_BOT)
        for _ in range(4):
            load_tracker.record_request(load_tracker.TRAFFIC_MONITORING)

        self.assertEqual(load_tracker.get_requests_per_minute(), 5)
        self.assertEqual(load_tracker.get_requests_per_hour(), 5)
        self.assertEqual(
            load_tracker.get_requests_per_hour(
                (load_tracker.TRAFFIC_MONITORING,)
            ),
            4,
        )
        self.assertEqual(
            load_tracker.get_traffic_breakdown(),
            {
                load_tracker.TRAFFIC_USER: 3,
                load_tracker.TRAFFIC_BOT: 2,
                load_tracker.TRAFFIC_MONITORING: 4,
            },
        )

    def test_peak_per_minute(self):
        for _ in range(3):
            load_tracker.record_request()
        load_tracker.record_request(load_tracker.TRAFFIC_BOT)

        self.assertEqual(load_tracker.get_peak_per_minute(), 4)

    def test_unknown_traffic_class_counts_as_user(self):
        load_tracker.record_request('robot')

        self.assertEqual(
            load_tracker.get_traffic_breakdown()[load_tracker.TRAFFIC_USER], 1,
        )

    def test_data_appears_after_first_request(self):
        self.assertFalse(load_tracker.has_data())
        load_tracker.record_request()
        self.assertTrue(load_tracker.has_data())


class RequestClassificationTests(SimpleTestCase):
    """Трафик делится на user / bot / monitoring."""

    def setUp(self):
        load_tracker.reset()
        self.request_factory = RequestFactory()

    def tearDown(self):
        load_tracker.reset()

    def _request(self, path, user_agent='Mozilla/5.0 (Windows NT 10.0)'):
        return self.request_factory.get(
            path, HTTP_USER_AGENT=user_agent,
        )

    def test_monitoring_paths_classified(self):
        for path in middleware.MONITORING_PREFIXES:
            self.assertEqual(
                middleware.classify_request(self._request(path)),
                load_tracker.TRAFFIC_MONITORING,
                path,
            )

    def test_bot_user_agents_classified(self):
        for user_agent in (
            'Googlebot/2.1 (+http://www.google.com/bot.html)',
            'curl/8.4.0',
            'python-requests/2.31.0',
        ):
            self.assertEqual(
                middleware.classify_request(
                    self._request('/uip/', user_agent=user_agent)
                ),
                load_tracker.TRAFFIC_BOT,
                user_agent,
            )

    def test_human_request_classified_as_user(self):
        self.assertEqual(
            middleware.classify_request(self._request('/uip/')),
            load_tracker.TRAFFIC_USER,
        )

    def test_middleware_skips_static_and_counts_monitoring_separately(self):
        response = object()
        track = middleware.LoadTrackingMiddleware(lambda request: response)

        self.assertIs(track(self._request('/static/js/main.js')), response)
        self.assertEqual(load_tracker.get_requests_per_hour(None), 0)

        track(self._request('/uip/'))
        self.assertEqual(load_tracker.get_requests_per_minute(None), 1)
        self.assertEqual(load_tracker.get_requests_per_minute(), 1)

        track(self._request('/scheduler/status/'))
        self.assertEqual(load_tracker.get_requests_per_minute(None), 2)
        self.assertEqual(load_tracker.get_requests_per_minute(), 1)


class RequestMetricsTests(TestCase):
    """Минутные агрегаты запросов: запись, UPSERT, сводка, ряд, очистка."""

    def setUp(self):
        metrics.reset()
        load_tracker.reset()
        RequestMetric.objects.all().delete()

    def tearDown(self):
        metrics.reset()
        load_tracker.reset()

    def _record(self, **overrides):
        params = {
            'group': 'uip_list',
            'method': 'GET',
            'status_class': '2xx',
            'traffic_class': load_tracker.TRAFFIC_USER,
            'duration_ms': 100,
        }
        params.update(overrides)
        metrics.record_request_metric(**params)

    def _row(self, *, group='uip_list', minutes_ago=1, count=1,
             traffic_class=load_tracker.TRAFFIC_USER, errors=0,
             duration=100):
        bucket = (
            timezone.now().replace(second=0, microsecond=0)
            - timedelta(minutes=minutes_ago)
        )
        values = {
            'bucket_start': bucket,
            'group': group,
            'method': 'GET',
            'status_class': '2xx',
            'traffic_class': traffic_class,
            'count': count,
            'errors': errors,
            'slow_count': 0,
            'total_ms': duration * count,
            'max_ms': duration,
            'db_ms': 10 * count,
            'db_queries': count,
            'peak_inflight': 3,
        }
        values.update({field: 0 for field in metrics.HISTOGRAM_FIELDS})
        values[metrics._histogram_field(duration)] = count
        return RequestMetric.objects.create(**values)

    def test_flush_writes_aggregated_row(self):
        self._record(duration_ms=40, db_ms=10, db_queries=2)
        self._record(duration_ms=300)

        self.assertEqual(metrics.pending_count(), 1)
        saved = metrics.flush_metrics()

        self.assertEqual(saved, 1)
        row = RequestMetric.objects.get()
        self.assertEqual(row.count, 2)
        self.assertEqual(row.total_ms, 340)
        self.assertEqual(row.max_ms, 300)
        self.assertEqual(row.db_ms, 10)
        self.assertEqual(row.db_queries, 2)
        self.assertEqual(row.h_lt50, 1)
        self.assertEqual(row.h_lt500, 1)

    def test_second_flush_increments_existing_row(self):
        self._record()
        metrics.flush_metrics()

        self._record(duration_ms=1200)
        metrics.flush_metrics()

        row = RequestMetric.objects.get()
        self.assertEqual(row.count, 2)
        self.assertEqual(row.slow_count, 1)
        self.assertEqual(row.max_ms, 1200)
        self.assertEqual(row.h_lt2000, 1)

    def test_5xx_increments_errors(self):
        self._record(status_class='5xx', duration_ms=1500)
        metrics.flush_metrics()

        row = RequestMetric.objects.get()
        self.assertEqual(row.errors, 1)
        self.assertEqual(row.slow_count, 1)

    def test_traffic_classes_are_separate_rows(self):
        self._record()
        self._record(
            group='scheduler_status',
            traffic_class=load_tracker.TRAFFIC_MONITORING,
        )
        metrics.flush_metrics()

        self.assertEqual(RequestMetric.objects.count(), 2)
        self.assertEqual(
            RequestMetric.objects.filter(
                traffic_class=load_tracker.TRAFFIC_MONITORING
            ).count(),
            1,
        )

    def test_window_stats_sums_traffic_and_errors(self):
        self._row(count=3, duration=100)
        self._row(count=2, traffic_class=load_tracker.TRAFFIC_BOT)
        self._row(count=4, traffic_class=load_tracker.TRAFFIC_MONITORING)
        self._row(count=1, errors=1, duration=5000, minutes_ago=2)

        stats = metrics.get_window_stats(60)

        self.assertEqual(stats['user'], 4)
        self.assertEqual(stats['bot'], 2)
        self.assertEqual(stats['monitoring'], 4)
        self.assertEqual(stats['errors'], 1)
        self.assertAlmostEqual(stats['error_rate'], 1 / 6)
        self.assertEqual(stats['source'], 'db')
        self.assertTrue(stats['has_data'])

    def test_p95_from_histogram(self):
        self._row(group='fast', count=94, duration=40)
        self._row(group='slow', count=6, duration=5000)

        stats = metrics.get_window_stats(60)

        self.assertEqual(stats['p95_ms'], 5000)

    def test_series_zero_fills_buckets(self):
        self._row(count=2)

        series = metrics.get_series('1h')

        self.assertEqual(series['resolution'], 'minute')
        self.assertEqual(len(series['points']), 61)
        self.assertEqual(
            sum(point['user'] for point in series['points']), 2,
        )

    def test_top_groups_sorted_by_count(self):
        self._row(group='uip_list', count=5)
        self._row(group='search', count=2)

        top = metrics.get_top_groups('1h')

        self.assertEqual(top['by_count'][0]['group'], 'uip_list')
        self.assertEqual(top['by_count'][0]['count'], 5)

    @override_settings(REQUEST_METRICS_RETENTION_DAYS=14)
    def test_prune_removes_old_rows(self):
        self._row(minutes_ago=60 * 24 * 20)
        self._row(minutes_ago=1)

        result = metrics.prune_old_metrics()

        self.assertFalse(result['is_error'])
        self.assertEqual(result['deleted'], 1)
        self.assertEqual(RequestMetric.objects.count(), 1)

    def test_falls_back_to_live_counters_without_table(self):
        load_tracker.record_request()

        with patch.object(metrics, '_safe_per_class', return_value=None):
            stats = metrics.get_window_stats(60)

        self.assertEqual(stats['source'], 'memory')
        self.assertEqual(stats['user'], 1)
        self.assertTrue(stats['has_data'])

    def test_middleware_records_metric(self):
        class _Response:
            status_code = 200

        track = middleware.LoadTrackingMiddleware(lambda request: _Response())
        self.assertIsNotNone(
            track(RequestFactory().get('/uip/', HTTP_USER_AGENT='Mozilla/5.0'))
        )

        self.assertEqual(metrics.pending_count(), 1)
        metrics.flush_metrics()
        row = RequestMetric.objects.get()
        self.assertEqual(row.group, 'uip')
        self.assertEqual(row.method, 'GET')
        self.assertEqual(row.status_class, '2xx')
        self.assertEqual(row.traffic_class, load_tracker.TRAFFIC_USER)

    def test_middleware_records_500_on_exception(self):
        def _boom(request):
            raise RuntimeError('boom')

        track = middleware.LoadTrackingMiddleware(_boom)
        with self.assertRaises(RuntimeError):
            track(RequestFactory().get('/uip/'))

        metrics.flush_metrics()
        row = RequestMetric.objects.get()
        self.assertEqual(row.status_class, '5xx')
        self.assertEqual(row.errors, 1)

    def test_middleware_maps_http_exceptions_to_4xx(self):
        from django.core.exceptions import PermissionDenied
        from django.http import Http404

        def _not_found(request):
            raise Http404('нет')

        def _denied(request):
            raise PermissionDenied('нельзя')

        cases = (
            (_not_found, Http404, '404'),
            (_denied, PermissionDenied, '403'),
        )
        for handler, exception, label in cases:
            with self.subTest(label):
                metrics.reset()
                RequestMetric.objects.all().delete()
                track = middleware.LoadTrackingMiddleware(handler)
                with self.assertRaises(exception):
                    track(RequestFactory().get('/uip/'))
                metrics.flush_metrics()
                row = RequestMetric.objects.get()
                self.assertEqual(row.status_class, '4xx')
                self.assertEqual(row.errors, 0)


class ExternalMetricsTests(TestCase):
    """Исходящие вызовы: аккумулятор, UPSERT, сводка по сервисам, health-чек."""

    def setUp(self):
        metrics.reset()
        RequestMetric.objects.all().delete()
        ExternalCallMetric.objects.all().delete()

    def tearDown(self):
        metrics.reset()

    def _row(self, *, service='cz', method='GET', outcome='ok',
             minutes_ago=1, count=1, errors=0, duration=100, endpoint=''):
        bucket = (
            timezone.now().replace(second=0, microsecond=0)
            - timedelta(minutes=minutes_ago)
        )
        values = {
            'bucket_start': bucket,
            'service': service,
            'method': method,
            'outcome': outcome,
            'endpoint': endpoint,
            'count': count,
            'errors': errors,
            'total_ms': duration * count,
            'max_ms': duration,
        }
        values.update({field: 0 for field in metrics.HISTOGRAM_FIELDS})
        values[metrics._histogram_field(duration)] = count
        return ExternalCallMetric.objects.create(**values)

    def test_flush_writes_external_rows(self):
        metrics.record_external_call(
            service='cz', method='POST', outcome='ok', duration_ms=120,
        )
        metrics.record_external_call(
            service='cz', method='POST', outcome='ok', duration_ms=80,
        )
        metrics.record_external_call(
            service='factory', method='GET', outcome='timeout',
            duration_ms=30000,
        )

        self.assertEqual(metrics.pending_external_count(), 2)
        self.assertEqual(metrics.flush_metrics(), 0)
        self.assertEqual(metrics.pending_external_count(), 0)

        cz = ExternalCallMetric.objects.get(service='cz')
        self.assertEqual(cz.count, 2)
        self.assertEqual(cz.errors, 0)
        self.assertEqual(cz.total_ms, 200)
        self.assertEqual(cz.max_ms, 120)

        factory = ExternalCallMetric.objects.get(service='factory')
        self.assertEqual(factory.count, 1)
        self.assertEqual(factory.errors, 1)

    def test_second_flush_increments_external_row(self):
        metrics.record_external_call(
            service='suz', method='GET', outcome='ok', duration_ms=50,
        )
        metrics.flush_metrics()
        metrics.record_external_call(
            service='suz', method='GET', outcome='ok', duration_ms=70,
        )
        metrics.flush_metrics()

        row = ExternalCallMetric.objects.get()
        self.assertEqual(row.count, 2)
        self.assertEqual(row.total_ms, 120)
        self.assertEqual(row.max_ms, 70)

    def test_external_stats_by_service(self):
        self._row(service='cz', count=4, duration=100)
        self._row(
            service='factory', outcome='timeout', count=1,
            errors=1, duration=30000,
        )

        stats = metrics.get_external_stats(60)

        self.assertEqual(stats['total'], 5)
        self.assertEqual(stats['errors'], 1)
        self.assertTrue(stats['has_data'])
        self.assertEqual(stats['source'], 'db')
        first = stats['services'][0]
        self.assertEqual(first['service'], 'cz')
        self.assertEqual(first['label'], 'Честный Знак (True API)')
        self.assertEqual(first['avg_ms'], 100)
        factory = stats['services'][1]
        self.assertEqual(factory['p95_ms'], 30000)
        self.assertEqual(factory['error_rate'], 1.0)

    def test_external_check_flags_errors(self):
        self._row(service='cz', outcome='5xx', count=10, errors=6)
        # Мало вызовов — доля ошибок не оценивается.
        self._row(service='suz', outcome='5xx', count=2, errors=2)

        check = metrics.external_check(60)

        self.assertFalse(check['ok'])
        self.assertIn('Честный Знак', check['message'])
        self.assertEqual(check['details']['problems'], ['cz'])

    def test_external_check_without_data_is_ok(self):
        check = metrics.external_check(60)

        self.assertTrue(check['ok'])
        self.assertIn('Нет данных', check['message'])

    def test_flush_keeps_endpoint(self):
        metrics.record_external_call(
            service='factory', method='GET', outcome='404',
            duration_ms=10, endpoint='/codes/api/get_codes_by_task/',
        )
        metrics.flush_metrics()

        row = ExternalCallMetric.objects.get()
        self.assertEqual(row.endpoint, '/codes/api/get_codes_by_task/')
        self.assertEqual(row.errors, 1)

    def test_external_check_explains_outcome_and_endpoint(self):
        self._row(
            service='factory', method='GET', outcome='404', count=8,
            errors=8, endpoint='/codes/api/get_codes_by_task/',
        )
        self._row(
            service='factory', method='GET', outcome='timeout', count=1,
            errors=1, endpoint='/codes/api/get_any_code_for_task/{id}/',
        )
        self._row(
            service='factory', method='GET', outcome='ok', count=10,
            endpoint='/codes/api/get_codes_by_task/',
        )

        check = metrics.external_check(60)

        self.assertFalse(check['ok'])
        self.assertIn('404 — 8', check['message'])
        self.assertIn('таймаут — 1', check['message'])
        self.assertIn('/codes/api/get_codes_by_task/', check['message'])

        factory = check['details']['services'][0]
        self.assertEqual(factory['service'], 'factory')
        self.assertEqual(factory['error_outcomes'][0]['outcome'], '404')
        self.assertEqual(
            factory['top_error_endpoint']['endpoint'],
            '/codes/api/get_codes_by_task/',
        )


class ExternalEndpointLabelTests(SimpleTestCase):
    """Эндпоинт в метриках — шаблон пути без uuid/номеров."""

    def test_uuid_segment_is_normalized(self):
        label = external_http.endpoint_label(
            'http://10.0.0.1:8000/codes/api/get_any_code_for_task/'
            '0f8b6a3e-1c2d-4e5f-8a9b-0c1d2e3f4a5b/'
        )

        self.assertEqual(
            label, '/codes/api/get_any_code_for_task/{id}/',
        )

    def test_number_segment_and_query_are_ignored(self):
        label = external_http.endpoint_label(
            'http://factory/codes/12345?gtin=04601751030375'
        )

        self.assertEqual(label, '/codes/{id}')

    def test_empty_url_returns_root(self):
        self.assertEqual(external_http.endpoint_label(''), '/')

    @patch('app_helper.external_http.metrics.record_external_call')
    @patch('app_helper.external_http.requests.request')
    def test_request_records_exact_status_and_endpoint(self, request, record):
        request.return_value.status_code = 404

        external_http.get('factory', 'http://factory/codes/api/task/abc/')

        kwargs = record.call_args.kwargs
        self.assertEqual(kwargs['outcome'], '404')
        self.assertEqual(kwargs['service'], 'factory')
        self.assertEqual(kwargs['endpoint'], '/codes/api/task/abc/')


class QueueAndSystemMetricsTests(TestCase):
    """Очередь django-tasks-db и сэмплы инфраструктуры."""

    def setUp(self):
        metrics.reset()
        SystemMetric.objects.all().delete()
        self.TaskResultStatus = self._task_status()
        self.DBTaskResult = self._db_task_model()
        self.DBTaskResult.objects.all().delete()

    def tearDown(self):
        metrics.reset()
        self.DBTaskResult.objects.all().delete()

    @staticmethod
    def _task_status():
        from django_tasks.base import TaskResultStatus
        return TaskResultStatus

    @staticmethod
    def _db_task_model():
        from django_tasks_db.models import DBTaskResult
        return DBTaskResult

    def _task(self, status, *, ready_age_seconds=0, duration_ms=None):
        now = timezone.now()
        task = self.DBTaskResult.objects.create(
            status=status,
            args_kwargs={},
            task_path='app_scheduler.tasks.some_task',
            backend_name='default',
            queue_name='default',
            run_after=now,
        )
        if ready_age_seconds:
            self.DBTaskResult.objects.filter(pk=task.pk).update(
                enqueued_at=now - timedelta(seconds=ready_age_seconds),
            )
        if duration_ms is not None:
            self.DBTaskResult.objects.filter(pk=task.pk).update(
                started_at=now - timedelta(milliseconds=duration_ms),
                finished_at=now,
            )
        return task

    def test_queue_stats(self):
        self._task(self.TaskResultStatus.READY, ready_age_seconds=120)
        self._task(self.TaskResultStatus.RUNNING)
        self._task(self.TaskResultStatus.SUCCESSFUL, duration_ms=500)
        self._task(self.TaskResultStatus.FAILED, duration_ms=1500)

        stats = metrics.get_queue_stats()

        self.assertTrue(stats['available'])
        self.assertEqual(stats['ready'], 1)
        self.assertEqual(stats['running'], 1)
        self.assertEqual(stats['failed_hour'], 1)
        self.assertEqual(stats['finished_hour'], 2)
        self.assertGreaterEqual(stats['oldest_ready_age_seconds'], 120)
        self.assertEqual(stats['p95_duration_ms'], 1500)

    def test_queue_check_flags_stale_task(self):
        self._task(self.TaskResultStatus.READY, ready_age_seconds=3600)

        with override_settings(
            QUEUE_MAX_READY=20, QUEUE_MAX_READY_AGE_SECONDS=600,
        ):
            check = metrics.queue_check()

        self.assertFalse(check['ok'])
        self.assertIn('мин', check['message'])

    def test_queue_check_ok(self):
        self._task(self.TaskResultStatus.READY)

        with override_settings(
            QUEUE_MAX_READY=20, QUEUE_MAX_READY_AGE_SECONDS=600,
            QUEUE_MAX_FAILED_PER_HOUR=5,
        ):
            check = metrics.queue_check()

        self.assertTrue(check['ok'])

    def test_queue_check_flags_failures(self):
        for _ in range(6):
            self._task(self.TaskResultStatus.FAILED, duration_ms=100)

        with override_settings(QUEUE_MAX_FAILED_PER_HOUR=5):
            check = metrics.queue_check()

        self.assertFalse(check['ok'])
        self.assertIn('Ошибок', check['message'])

    def test_db_connections_check(self):
        result = service_helper.check_db_connections()

        self.assertTrue(result['is_ok'])
        self.assertIn('Соединений PG', result['message'])

    def test_db_connections_check_flags_high_load(self):
        with patch.object(
            service_helper, '_db_connection_counts', return_value=(95, 100),
        ), override_settings(DB_CONNECTIONS_THRESHOLD=0.8):
            result = service_helper.check_db_connections()

        self.assertFalse(result['is_ok'])
        self.assertIn('95', result['message'])

    def _system_row(self, metric, *, kind='gauge', value=1.0,
                    minutes_ago=1, max_value=None):
        bucket = (
            timezone.now().replace(second=0, microsecond=0)
            - timedelta(minutes=minutes_ago)
        )
        return SystemMetric.objects.create(
            bucket_start=bucket,
            metric=metric,
            kind=kind,
            value=value,
            max_value=value if max_value is None else max_value,
        )

    def test_system_stats_gauges_and_counters(self):
        self._system_row('cpu_percent', value=40.0)
        self._system_row('cpu_percent', value=70.0, minutes_ago=2)
        self._system_row('db_xact_commit', kind='counter', value=10)
        self._system_row(
            'db_xact_commit', kind='counter', value=5, minutes_ago=2,
        )
        self._system_row(
            'db_temp_bytes', kind='counter', value=2 * 1024 * 1024,
        )

        stats = metrics.get_system_stats(60)
        by_name = {item['metric']: item for item in stats['metrics']}

        self.assertEqual(by_name['cpu_percent']['value'], 40.0)
        self.assertEqual(by_name['cpu_percent']['max'], 70.0)
        self.assertEqual(by_name['db_xact_commit']['value'], 15.0)
        self.assertEqual(by_name['db_temp_bytes']['value'], 2.0)
        self.assertEqual(by_name['db_temp_bytes']['unit'], 'МБ/мин')
        self.assertTrue(stats['has_data'])

    def test_system_series_for_host_metrics(self):
        self._system_row('cpu_percent', value=40.0)
        self._system_row('mem_percent', value=55.0)

        series = metrics.get_system_series('1h')

        self.assertTrue(series['available'])
        point = series['points'][0]
        self.assertEqual(point['cpu_percent'], 40.0)
        self.assertEqual(point['mem_percent'], 55.0)

    def test_flush_stores_system_samples(self):
        samples = {
            'cpu_percent': ('gauge', 42.0),
            'db_xact_commit': ('counter', 7),
        }
        metrics.record_request_metric(
            group='uip_list', method='GET', status_class='2xx',
            traffic_class=load_tracker.TRAFFIC_USER, duration_ms=10,
        )
        with patch.object(
            metrics.system_metrics, 'collect_samples', return_value=samples,
        ):
            self.assertEqual(metrics.flush_metrics(), 1)

        cpu = SystemMetric.objects.get(metric='cpu_percent')
        self.assertEqual(cpu.kind, 'gauge')
        self.assertEqual(cpu.value, 42.0)
        commits = SystemMetric.objects.get(metric='db_xact_commit')
        self.assertEqual(commits.kind, 'counter')
        self.assertEqual(commits.value, 7.0)


class LogNoiseTests(TestCase):
    """Повторяющиеся сбои/проверки не забивают журнал и stdout."""

    def setUp(self):
        cache.clear()
        EventLog.objects.all().delete()
        service_helper._failure_log_at.clear()

    def test_duplicate_external_failures_written_once(self):
        from app_helper.external import safe_external_call

        def boom():
            raise RuntimeError('down')

        with self.assertLogs('app_helper.external', level='ERROR'):
            safe_external_call(boom)
        safe_external_call(boom)  # подавлено дедупликацией

        self.assertEqual(EventLog.objects.count(), 1)

    def test_check_failure_logged_once(self):
        with self.assertLogs(
            'app_helper.service_helper', level='WARNING',
        ) as logs:
            service_helper._log_check_failure('http://down/', 'err')
            service_helper._log_check_failure('http://down/', 'err')

        self.assertEqual(len(logs.output), 1)


class SessionCookieNameTests(SimpleTestCase):
    """SESSION_COOKIE_NAME по умолчанию стабилен между перезапусками."""

    def test_default_name_is_stable_across_reload(self):
        import importlib
        import os

        import config.settings as settings_module

        clean_env = {
            key: value
            for key, value in os.environ.items()
            if key != 'SESSION_COOKIE_NAME'
        }
        with patch.dict(os.environ, clean_env, clear=True), patch(
            'dotenv.load_dotenv',
        ):
            importlib.reload(settings_module)
            first = settings_module.SESSION_COOKIE_NAME
            importlib.reload(settings_module)
            second = settings_module.SESSION_COOKIE_NAME

        self.assertEqual(first, 'pms_sessionid')
        self.assertEqual(first, second)


class DiagnoseServiceTests(TestCase):
    """Самодиагностика: у проверок есть читаемое имя и понятное сообщение."""

    def _diagnose(self):
        with patch.object(
            service_helper, 'check_suz_token',
            return_value={'is_ok': True, 'message': 'Токен действителен'},
        ), patch.object(
            service_helper, 'check_signatures',
            return_value={'is_ok': True, 'message': 'HTTP 200'},
        ), patch.object(
            service_helper, 'check_factories',
            return_value={'is_ok': True, 'factories': []},
        ), patch.object(
            service_helper, 'get_load_stats',
            return_value={
                'requests_per_minute': 300,
                'requests_per_hour': 20000,
                'threshold_per_hour': 10000,
                'is_high_load': True,
                'message': (
                    'Высокая нагрузка: 20000 запросов/час '
                    '(порог 10000), за минуту 300'
                ),
            },
        ):
            return service_helper.diagnose_service()

    def test_load_check_has_readable_name_and_message(self):
        diagnosis = self._diagnose()

        load = diagnosis['checks']['load']
        self.assertEqual(load['name'], 'Нагрузка')
        self.assertFalse(load['ok'])
        self.assertIn('20000', load['message'])
        self.assertFalse(diagnosis['is_available'])

    def test_summary_lists_readable_names(self):
        diagnosis = self._diagnose()

        summary = diagnosis['checks']['summary']
        self.assertIn('Нагрузка', summary['message'])

    def test_load_without_data_is_not_a_failure(self):
        with patch.object(
            service_helper, 'check_suz_token',
            return_value={'is_ok': True, 'message': 'Токен действителен'},
        ), patch.object(
            service_helper, 'check_signatures',
            return_value={'is_ok': True, 'message': 'HTTP 200'},
        ), patch.object(
            service_helper, 'check_factories',
            return_value={'is_ok': True, 'factories': []},
        ), patch.object(
            service_helper, 'get_load_stats',
            return_value={
                'requests_per_minute': 0,
                'requests_per_hour': 0,
                'threshold_per_hour': 50_000,
                'is_high_load': False,
                'has_data': False,
                'message': 'Нагрузка: нет данных — счётчики в web-процессе',
            },
        ):
            diagnosis = service_helper.diagnose_service()

        load = diagnosis['checks']['load']
        self.assertTrue(load['ok'])
        self.assertFalse(load['has_data'])
        self.assertIn('нет данных', load['message'])

    def test_every_check_has_name_and_message(self):
        diagnosis = self._diagnose()

        for key, check in diagnosis['checks'].items():
            self.assertTrue(check.get('name'), key)
            self.assertTrue(check.get('message'), key)


class AsgiCancelledErrorLoggingTests(SimpleTestCase):
    """Отмена запроса при остановке сервера — INFO, а не traceback."""

    def test_cancelled_error_logged_as_info(self):
        import asyncio
        from unittest.mock import Mock

        from config.asgi import _handle_loop_exception

        loop = Mock()
        with self.assertLogs('uvicorn.error', level='INFO') as logs:
            _handle_loop_exception(loop, {
                'message': 'CancelledError exception in shielded future',
                'exception': asyncio.CancelledError(),
            })

        loop.default_exception_handler.assert_not_called()
        self.assertIn('Остановка сервера', logs.output[0])

    def test_other_errors_go_to_default_handler(self):
        from unittest.mock import Mock

        from config.asgi import _handle_loop_exception

        loop = Mock()
        context = {'message': 'boom', 'exception': RuntimeError('boom')}
        _handle_loop_exception(loop, context)

        loop.default_exception_handler.assert_called_once_with(context)
