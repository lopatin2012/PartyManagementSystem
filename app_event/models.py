# app_event/models.py

from django.db import models


class EventLog(models.Model):
    """События системы для живой ленты."""

    LEVEL_CHOICES = [
        ('info', 'Информация'),
        ('success', 'Успех'),
        ('warning', 'Предупреждение'),
        ('error', 'Ошибка'),
        ('critical', 'Критическая ошибка'),
    ]

    MODULE_CHOICES = [
        ('production', 'Производство'),
        ('cz', 'Честный Знак'),
        ('system', 'Система'),
        ('nk', 'Национальный каталог'),
    ]

    module = models.CharField(max_length=20, choices=MODULE_CHOICES, verbose_name='Модуль')
    level = models.CharField(max_length=20, choices=LEVEL_CHOICES, verbose_name='Уровень события')
    message = models.CharField(max_length=255, verbose_name='Сообщение')

    actor = models.CharField(
        max_length=100, blank=True, null=True, verbose_name='Инициатор'
    )
    metadata = models.JSONField(
        default=dict, blank=True, null=True, verbose_name='Дополнительный контекст'
    )

    created_at = models.DateTimeField(auto_now_add=True, verbose_name='Дата создания записи')

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['-created_at']),
            models.Index(fields=['module', '-created_at']),
        ]
        verbose_name = 'Событие системы'
        verbose_name_plural = 'События системы'

    def __str__(self):
        return f'[{self.module}] {self.level}: {self.message}'


class HealthCheck(models.Model):
    """История проверок состояния внешних сервисов и системы."""

    LEVEL_CHOICES = [
        ('ok', 'В норме'),
        ('warning', 'Предупреждение'),
        ('critical', 'Критическая ошибка'),
    ]

    service = models.CharField(
        max_length=50, db_index=True, verbose_name='Сервис'
    )
    name = models.CharField(max_length=100, verbose_name='Название проверки')
    is_ok = models.BooleanField(db_index=True, verbose_name='Доступен')
    level = models.CharField(
        max_length=20, choices=LEVEL_CHOICES, default='ok', verbose_name='Уровень'
    )
    message = models.CharField(
        max_length=255, blank=True, default='', verbose_name='Сообщение'
    )
    details = models.JSONField(
        default=dict, blank=True, verbose_name='Детали'
    )
    checked_at = models.DateTimeField(auto_now_add=True, verbose_name='Время проверки')

    class Meta:
        ordering = ['-checked_at']
        indexes = [
            models.Index(fields=['service', '-checked_at']),
            models.Index(fields=['-checked_at']),
        ]
        verbose_name = 'Проверка состояния'
        verbose_name_plural = 'Проверки состояния'

    def __str__(self):
        return f'{self.name}: {"OK" if self.is_ok else "FAIL"} ({self.level})'


class RequestMetric(models.Model):
    """
    Агрегат HTTP-запросов за минуту (наблюдаемость нагрузки).

    Одна строка — один endpoint (`group`), метод, класс статуса и класс
    трафика за минуту. Пишется пачками из `app_helper/metrics.py` через
    UPSERT (ON CONFLICT DO UPDATE с прибавлением), поэтому несколько
    процессов и рестарты складываются корректно.

    Гистограмма времени ответа (`h_*`) позволяет считать p95 без хранения
    каждого запроса.
    """

    bucket_start = models.DateTimeField(
        db_index=True, verbose_name='Начало минуты',
    )
    group = models.CharField(
        max_length=100, verbose_name='Endpoint',
        help_text='url_name / view_name или первый сегмент пути',
    )
    method = models.CharField(max_length=10, verbose_name='Метод')
    status_class = models.CharField(
        max_length=5, verbose_name='Класс статуса',
        help_text='2xx / 3xx / 4xx / 5xx',
    )
    traffic_class = models.CharField(
        max_length=12, verbose_name='Класс трафика',
        help_text='user / bot / monitoring',
    )

    count = models.PositiveIntegerField(default=0, verbose_name='Запросов')
    errors = models.PositiveIntegerField(default=0, verbose_name='Ошибок 5xx')
    slow_count = models.PositiveIntegerField(
        default=0, verbose_name='Медленных (>= 1 c)',
    )
    total_ms = models.BigIntegerField(default=0, verbose_name='Суммарное время, мс')
    max_ms = models.IntegerField(default=0, verbose_name='Максимальное время, мс')
    db_ms = models.BigIntegerField(default=0, verbose_name='Время в БД, мс')
    db_queries = models.BigIntegerField(default=0, verbose_name='Запросов к БД')
    peak_inflight = models.PositiveIntegerField(
        default=0, verbose_name='Пик параллельных запросов',
    )

    h_lt50 = models.PositiveIntegerField(default=0)
    h_lt100 = models.PositiveIntegerField(default=0)
    h_lt250 = models.PositiveIntegerField(default=0)
    h_lt500 = models.PositiveIntegerField(default=0)
    h_lt1000 = models.PositiveIntegerField(default=0)
    h_lt2000 = models.PositiveIntegerField(default=0)
    h_lt5000 = models.PositiveIntegerField(default=0)
    h_gte5000 = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['-bucket_start']
        constraints = [
            models.UniqueConstraint(
                fields=[
                    'bucket_start', 'group', 'method',
                    'status_class', 'traffic_class',
                ],
                name='reqmetric_unique_key',
            ),
        ]
        indexes = [
            models.Index(
                fields=['bucket_start', 'traffic_class'],
                name='reqmetric_bucket_traffic',
            ),
            models.Index(
                fields=['bucket_start', 'group'],
                name='reqmetric_bucket_group',
            ),
        ]
        verbose_name = 'Метрика запросов'
        verbose_name_plural = 'Метрики запросов'

    def __str__(self):
        return (
            f'{self.bucket_start:%d.%m.%Y %H:%M} {self.group} '
            f'{self.method} {self.status_class}: {self.count}'
        )


class NotificationRecipient(models.Model):
    """
    Настройка рассылки: какая группа получает алерты.

    Email-адреса берутся из `User.email` активных пользователей выбранной
    группы (отдельного поля адреса нет).
    """

    group = models.OneToOneField(
        to='auth.Group',
        on_delete=models.CASCADE,
        related_name='notification_recipient',
        verbose_name='Группа',
        help_text='Группа, участники которой получают алерты (например, «Мониторинг»)',
    )
    is_active = models.BooleanField(default=True, verbose_name='Активен')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='Создано')

    class Meta:
        ordering = ['group__name']
        verbose_name = 'Получатель уведомлений'
        verbose_name_plural = 'Получатели уведомлений'

    def __str__(self):
        status = '' if self.is_active else ' [выкл]'
        return f'{self.group.name}{status}'
