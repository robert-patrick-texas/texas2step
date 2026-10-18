# Enterprise Network Speedtest

Self-service internal speedtest website with streaming download/upload (no disk I/O), single- and multi-flow tests, latency/jitter/loss probes, real-time monitor graph, dual-stack IP reporting, and multi-location dropdown.

## Files

- `app.py` – FastAPI application
- `static/index.html` – Frontend UI
- `requirements.txt` – Python dependencies
- `nginx-speedtest.conf.example` – Nginx reverse-proxy configuration

## Deployment on Ubuntu 26.04 LTS (nginx already installed)

1. Install Python dependencies and create a virtual environment:

```bash
sudo apt update
sudo apt install python3-venv python3-pip -y
cd /opt
sudo mkdir -p speedtest
sudo chown $USER:$USER speedtest
cd speedtest
python3 -m venv venv
source venv/bin/activate
pip install -r /path/to/requirements.txt
```

2. Copy the application files:

```bash
cp app.py static/ requirements.txt /opt/speedtest/
```

3. Edit `app.py` and update the `TEST_LOCATIONS` list if you have multiple servers.

4. Create a systemd service (`/etc/systemd/system/speedtest.service`):

```ini
[Unit]
Description=Enterprise Speedtest
After=network.target

[Service]
User=www-data
WorkingDirectory=/opt/speedtest
Environment="PATH=/opt/speedtest/venv/bin"
ExecStart=/opt/speedtest/venv/bin/uvicorn app:app --host 127.0.0.1 --port 8000
Restart=always

[Install]
WantedBy=multi-user.target
```

5. Enable and start the service:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now speedtest
```

6. Configure nginx:

```bash
sudo cp nginx-speedtest.conf.example /etc/nginx/sites-available/speedtest
# Edit the file: set server_name and SSL certificate paths
sudo ln -s /etc/nginx/sites-available/speedtest /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

7. Open the site in a browser. For curl download tests:

```bash
curl -o /dev/null http://speedtest.internal.example.com/download?bytes=104857600
```

## Notes

- All data is streamed; nothing is written to disk.
- Packet loss figures are HTTP request stalls/timeouts (browsers cannot send raw ICMP).
- The frontend supports forcing IPv4 or IPv6 via the dropdown when the client is dual-stack (hostname must resolve to both families, or use literal addresses).
- Monitor mode aggregates five RTT samples every second and draws a live graph.
- HTML report is generated entirely client-side.
