"""Только проверка отдельного Compose Airpay; никаких build/up/run, миграций или pay."""
import json
from pathlib import Path
import subprocess


def validate(config):
    # Проверяем уже раскрытые Docker Compose настройки, не печатая секреты из env_file.
    services = config.get('services', {})
    if set(services) != {'airpay-api', 'airpay-migrate'} or config.get('name') != 'supplier-hub-airpay':
        raise ValueError('Use the standalone Airpay compose project only')
    if config.get('volumes'):
        raise ValueError('Airpay must not create database volumes')
    networks = config.get('networks', {})
    if len(networks) != 1 or not all(net.get('external') and net.get('name') for net in networks.values()):
        raise ValueError('Select exactly one existing external Hub network')
    for name, service in services.items():
        if service.get('depends_on'):
            raise ValueError('Airpay must not start dependencies')
        if str(service.get('environment', {}).get('AIRPAY_PAYMENTS_ENABLED', '')).lower() != 'false':
            raise ValueError('First rollout requires disabled Airpay payments')
        if not service.get('mem_limit') or not service.get('cpus'):
            raise ValueError('Set Airpay resource limits')
        if not service.get('environment', {}).get('DATABASE_URL'):
            raise ValueError('Configure Airpay database explicitly')
        if name == 'airpay-api':
            ports = service.get('ports', [])
            if len(ports) != 1 or ports[0].get('host_ip') != '127.0.0.1' or ports[0].get('target') != 8011:
                raise ValueError('Bind Airpay only to localhost on target 8011')
    return next(iter(networks.values()))['name']


def docker_json(args, cwd):
    # Ошибки Docker не выводим целиком, поскольку они могут включать подставленные настройки.
    result = subprocess.run(['docker', *args], cwd=cwd, capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise ValueError('Docker preflight failed; check dedicated env, Docker and network configuration')
    return json.loads(result.stdout)


def run(root):
    # Внешняя сеть только читается: её создание, перезапуск БД и запуск сервисов здесь невозможны.
    config = docker_json(['compose', '-f', 'docker-compose.airpay.yml', 'config', '--format', 'json'], root)
    network = validate(config)
    existing = docker_json(['network', 'inspect', network], root)
    if not existing or not existing[0].get('Containers'):
        raise ValueError('Selected network has no existing containers; verify the Hub database network')
    print('PASS: isolated Airpay services, payments disabled, resource limits and existing network. No services started.')
    print('Still required: verified backup, pending migration review, database reachability and deployed read-only smoke.')


if __name__ == '__main__':
    # Запускается на выбранном хосте вручную, из корня поставки Hub; не принимает команды развёртывания.
    try:
        run(Path.cwd())
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        raise SystemExit(str(exc) if isinstance(exc, ValueError) else 'Docker unavailable for Airpay preflight') from None
