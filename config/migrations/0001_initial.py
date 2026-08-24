import app_helper.models
import uuid_utils._uuid_utils
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
    ]

    operations = [
        migrations.CreateModel(
            name='ExternalService',
            fields=[
                ('id', app_helper.models.UUID7Field(default=uuid_utils._uuid_utils.uuid7, editable=False, help_text='Уникальный идентификатор', primary_key=True, serialize=False)),
                ('service_type', models.IntegerField(choices=[(1, 'Сервис подписей'), (2, 'WMS'), (3, 'Сервис печати')], unique=True, verbose_name='Тип сервиса', help_text='Тип внешнего сервиса. Должен быть уникальным.')),
                ('name', models.CharField(max_length=50, unique=True, verbose_name='Наименование')),
                ('photo', models.ImageField(blank=True, null=True, upload_to='factory_image', verbose_name='Фотография')),
                ('ip_address', models.GenericIPAddressField(verbose_name='IP-адрес сервиса', help_text='IP-адрес, на котором запущен внешний сервис')),
                ('port_address', models.IntegerField(verbose_name='Порт сервиса', help_text='Порт, на котором запущен внешний сервис')),
                ('is_active', models.BooleanField(default=True, verbose_name='Действующий')),
            ],
            options={
                'verbose_name': 'Внешний сервис',
                'verbose_name_plural': '1. Внешние сервисы',
                'ordering': ('-id',),
                'constraints': [models.UniqueConstraint(fields=('ip_address', 'port_address'), name='unique_ip_address_and_port_address')],
            },
        ),
    ]
