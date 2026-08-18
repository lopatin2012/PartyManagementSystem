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
