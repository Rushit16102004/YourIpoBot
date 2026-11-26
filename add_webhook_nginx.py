path = "/etc/nginx/sites-available/youripobot"
with open(path) as f:
    text = f.read()

target = "    location / {"
webhook_block = """    location /webhook {
        proxy_pass http://127.0.0.1:9001/webhook;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

"""

if "location /webhook" not in text:
    text = text.replace(target, webhook_block + target)
    with open(path, "w") as f:
        f.write(text)
    print("WEBHOOK_ROUTE_ADDED")
else:
    print("ALREADY_EXISTS")
