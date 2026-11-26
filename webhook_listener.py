from flask import Flask, request, jsonify
import subprocess
import hmac
import hashlib
import os

app = Flask(__name__)
SECRET = os.environ.get("DEPLOY_SECRET", "youripobot_secret_key_1610")

@app.route("/webhook", methods=["POST"])
def github_webhook():
    signature = request.headers.get("X-Hub-Signature-256")
    if signature:
        sha_name, sig = signature.split("=")
        mac = hmac.new(SECRET.encode(), msg=request.data, digestmod=hashlib.sha256)
        if not hmac.compare_digest(mac.hexdigest(), sig):
            return jsonify({"status": "forbidden", "reason": "invalid signature"}), 403
    
    # Run deploy in background
    subprocess.Popen(["/bin/bash", "/var/www/YourIpoBot/deploy.sh"])
    return jsonify({"status": "deploy_triggered", "timestamp": str(os.times())}), 200

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=9001)
