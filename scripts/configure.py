import secrets
from pathlib import Path
p=Path('.env')
if p.exists():
    raise SystemExit('.env already exists; refusing to overwrite settings')
p.write_text('ADMIN_USER=admin\nADMIN_PASSWORD='+secrets.token_urlsafe(24)+'\nCSRF_TOKEN='+secrets.token_urlsafe(32)+'\nIMPORT_TOKEN='+secrets.token_urlsafe(32)+'\nNTFY_TOPIC=product-watcher-'+secrets.token_hex(16)+'\nLAN_IP=127.0.0.1\nPORT=8765\nALLOWED_CIDRS=127.0.0.0/8\n')
p.chmod(0o600)
print('Created .env. Configure LAN_IP and ALLOWED_CIDRS before LAN deployment.')
