from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('sms', '0002_inbound_outbound_indexes_and_validators'),
    ]

    operations = [
        migrations.CreateModel(
            name='InboundWebhook',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=128)),
                ('url', models.URLField(max_length=512)),
                ('enabled', models.BooleanField(default=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
            ],
            options={
                'ordering': ['name'],
            },
        ),
    ]
