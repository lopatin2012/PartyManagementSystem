from django.db import models


class TaskProgress(models.Model):
    """
    Прогресс выполнения периодической задачи.

    Пишется воркером по ходу выполнения, читается веб-процессом в окне
    «Фоновые задачи»: позволяет видеть, что именно и в каком объёме сейчас
    делается. Одна строка на задачу.
    """

    task_name = models.CharField(
        max_length=100, unique=True, verbose_name='Задача'
    )
    phase = models.CharField(
        max_length=150, blank=True, default='', verbose_name='Этап'
    )
    current = models.IntegerField(
        null=True, blank=True, verbose_name='Обработано'
    )
    total = models.IntegerField(
        null=True, blank=True, verbose_name='Всего'
    )
    message = models.TextField(
        blank=True, default='', verbose_name='Сообщение'
    )
    updated_at = models.DateTimeField(
        auto_now=True, verbose_name='Обновлено'
    )

    class Meta:
        verbose_name = 'Прогресс задачи'
        verbose_name_plural = 'Прогресс задач'
        ordering = ('task_name',)

    def __str__(self):
        return f'{self.task_name}: {self.phase} ({self.current}/{self.total})'
