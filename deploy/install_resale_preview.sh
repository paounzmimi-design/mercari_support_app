#!/usr/bin/env bash
# Install an isolated, loopback-only preview. No live payment keys or nginx edits.
set -euo pipefail
preview=/root/mercari_resale_preview
unit=resale-preview.service
if [ "$(id -u)" != 0 ] || [ "$(pwd -P)" != "$preview" ]; then
    echo '停止：rootで /root/mercari_resale_preview から実行してください'
    exit 1
fi
if [ -e .venv ] || [ -e .env-preview ] || [ -e /etc/systemd/system/$unit ]; then
    echo '停止：試作環境が既にあります。上書きしていません'
    exit 1
fi
if [ -n "$(ss -H -ltn 'sport = :5200')" ]; then
    echo '停止：5200ポートが使用中です。既存サービスは変更していません'
    exit 1
fi
python3 -m venv .venv
.venv/bin/python -m pip install -r resale-requirements.txt pytest
.venv/bin/python -m pytest -q tests/test_resale.py tests/test_resale_reconcile.py
.venv/bin/python - <<'PY'
import os
import secrets
from pathlib import Path
directory = Path('resale-private')
directory.mkdir(mode=0o700)
fd = os.open('.env-preview', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'w') as output:
    output.write('RESALE_SECRET_KEY=' + secrets.token_urlsafe(48) + '\n')
    output.write('RESALE_DATABASE=/root/mercari_resale_preview/resale-private/workbench.sqlite3\n')
    output.write('RESALE_STRIPE_TEST_KEY=\nRESALE_STRIPE_TEST_WEBHOOK_SECRET=\nRESALE_PUBLIC_URL=\n')
print('専用設定：作成済み（秘密は表示していません）')
PY
installed=0
rollback() {
    if [ "$installed" = 1 ]; then
        systemctl disable --now "$unit" >/dev/null 2>&1 || true
        rm -f "/etc/systemd/system/$unit"
        systemctl daemon-reload
        echo '停止：試作サービスの設置を取り消しました。専用フォルダは確認用に残しています'
    fi
}
trap rollback ERR
cat > "/etc/systemd/system/$unit" <<'UNIT'
[Unit]
Description=Isolated resale preview (loopback only, payments disabled)
After=network.target

[Service]
Type=simple
WorkingDirectory=/root/mercari_resale_preview
EnvironmentFile=/root/mercari_resale_preview/.env-preview
ExecStart=/root/mercari_resale_preview/.venv/bin/gunicorn --workers 1 --bind 127.0.0.1:5200 --timeout 30 resale.web:create_app()
Restart=on-failure
RestartSec=5
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=/root/mercari_resale_preview/resale-private

[Install]
WantedBy=multi-user.target
UNIT
installed=1
systemctl daemon-reload
systemctl enable --now "$unit"
ready=0
for attempt in 1 2 3 4 5 6 7 8 9 10; do
    if curl -fsS --max-time 3 http://127.0.0.1:5200/healthz >/dev/null; then
        ready=1
        break
    fi
    sleep 2
done
test "$ready" = 1
curl -fsS --max-time 5 http://127.0.0.1:5200/healthz
curl -fsS --max-time 5 http://127.0.0.1:5200/ >/dev/null
systemctl is-active "$unit"
trap - ERR
echo 'TEST PREVIEW READY：試作のみ起動・決済停止・公開URL未設定'
