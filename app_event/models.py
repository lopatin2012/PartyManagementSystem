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
