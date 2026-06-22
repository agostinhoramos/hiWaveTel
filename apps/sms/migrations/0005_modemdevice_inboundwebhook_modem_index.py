from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('sms', '0004_drop_external_device_tables'),
    ]

    operations = [
        migrations.CreateModel(
            name='ModemDevice',
            fields=[
                ('modem_index', models.PositiveSmallIntegerField(primary_key=True, serialize=False)),
                ('dbus_path', models.CharField(blank=True, max_length=256)),
                ('enabled', models.BooleanField(default=True)),
                ('is_present', models.BooleanField(default=True)),
                ('first_detected_at', models.DateTimeField(auto_now_add=True)),
                ('last_detected_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'ordering': ['modem_index'],
            },
        ),
        migrations.AddField(
            model_name='inboundwebhook',
            name='modem_index',
            field=models.PositiveSmallIntegerField(db_index=True, default=0),
        ),
        migrations.AlterField(
            model_name='inboundwebhook',
            name='modem_index',
            field=models.PositiveSmallIntegerField(db_index=True),
        ),
        migrations.AlterModelOptions(
            name='inboundwebhook',
            options={'ordering': ['modem_index', 'name']},
        ),
        migrations.AddConstraint(
            model_name='inboundwebhook',
            constraint=models.UniqueConstraint(
                fields=('modem_index', 'url'),
                name='sms_inboundwebhook_modem_url_uniq',
            ),
        ),
    ]
