# YourIpoBot: Quantitative IPO Grey Market Predictor

Production-ready, automated IPO discovery, RHP extraction, live subscription monitoring, and Grey Market Premium (GMP) forecasting engine designed for **Oracle Cloud Infrastructure (OCI Always Free tier)**.

---

## 1. Directory Structure

```text
YourIpoBot/
├── companies/                          # Dedicated folder per company
│   ├── FOODLINK/                       # Raw RHP & Anchor PDFs
│   ├── FOODLINK_FB_HOLDINGS_.../       # Active tracking directory
│   │   ├── profile.json                # Price band, dates, issue size
│   │   ├── metrics.json                # Extracted RHP fundamentals
│   │   ├── prediction.json             # Latest predicted GMP & top peers
│   │   ├── rhp.pdf                     # Red Herring Prospectus
│   │   └── anchor.pdf                  # Anchor Allocation document
│   └── AIRPRO/, AJPACK/, ...           # Other companies
├── core/                               # Core Python engine
│   ├── downloader.py                   # Anti-block resilient PDF fetcher
│   ├── predictor.py                    # 5-Tier k-NN similarity engine
│   ├── storage.py                      # SQLite database & queue manager
│   └── pipeline.py                     # Daily timetable orchestrator
├── data/
│   ├── warehouse/ipo_history.db        # 3,634 verified benchmark IPOs
│   └── staging/ipo_merged_master.csv   # Historical feature dataset (78 cols)
├── web/                                # Production UI Dashboard
│   ├── static/css/style.css            # Dark modern trading interface
│   ├── static/js/app.js                # Live dynamic updates
│   ├── templates/dashboard.html        # Jinja2 dashboard template
│   └── app.py                          # Flask server & REST API
├── config.py                           # Central configuration & thresholds
├── Dockerfile                          # Production container configuration
├── docker-compose.yml                  # OCI 1-click deployment
└── requirements.txt                    # Python dependencies
```

---

## 2. Daily Execution Schedule

| Time | Action | What It Does |
| :--- | :--- | :--- |
| **05:00 AM** | `job_morning_subscription_sync` | Syncs official Day 1 / Day 2 cumulative subscription from BSE/NSE. |
| **06:30 AM** | `job_morning_rhp_download` | Downloads RHP PDF for nearest upcoming IPO (max 1/day, polite jitter). |
| **03:30 PM - 04:00 PM** | `job_day3_live_rush` | Polls live Day 3 bids before the retail application cutoff. |
| **04:05 PM** | **CRITICAL GMP UPDATE** | ⭐ Generates actionable Final Day 3 GMP before 4:45 PM UPI mandate cutoff. |
| **06:30 PM** | `job_evening_ipo_detector` | Scans official exchange feeds for newly approved upcoming IPOs. |
| **07:30 PM** | `job_day_end_reconciliation` | Reconciles final Day 3 figures and commits to master warehouse. |
| **09:05 PM - 11:00 PM** | `job_night_anchor_worker` | Downloads Anchor list for Mainboard IPOs (skips SME; rate limit 1/hr). |

---

## 3. The 5-Tier Weight Structure

- **Subscription Momentum (QIB / NII / Total):** `30%`
- **Issue Size & Liquidity Bracket:** `20%`
- **Profitability & Margin Strength:** `25%`
- **Debt, Cash Flow & Dilution:** `15%`
- **Other Info & Governance (Board, Sector, Promoter stake):** `10%`

### Peer Selection Rule:
1. Filter candidates matching **$\ge 80\%$ composite similarity**.
2. **Priority 1:** Select peers matching $\ge 80\%$ within the **same sector / industry**.
3. **Priority 2:** If fewer than 5 exist in the same sector, fill remaining spots up to 5 from the highest-scoring cross-sector peers.
4. Compute weighted average: $\text{GMP} = \frac{\sum (S_i \times \text{GMP}_i)}{\sum S_i}$.

---

## 4. Oracle Cloud (OCI) Deployment Guide

### Option A: Direct systemd Service (Ubuntu 22.04 on OCI)
```bash
# 1. Clone repository to Oracle VM
git clone <YOUR_REPO_URL> /opt/YourIpoBot
cd /opt/YourIpoBot

# 2. Setup Python Virtual Environment
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 3. Create background service for Web Dashboard
sudo nano /etc/systemd/system/youripobot-web.service
```

Paste into service file:
```ini
[Unit]
Description=YourIpoBot Web Dashboard
After=network.target

[Service]
User=ubuntu
WorkingDirectory=/opt/YourIpoBot
ExecStart=/opt/YourIpoBot/venv/bin/python -m YourIpoBot.web.app
Restart=always

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now youripobot-web
```

### Option B: Docker Container
```bash
docker build -t youripobot:latest -f YourIpoBot/Dockerfile .
docker run -d -p 8000:8000 -v /opt/youripobot_data:/app/YourIpoBot/companies --name youripobot youripobot:latest
```

---

## 5. Security & Anti-Bot Protection
- Rotating User-Agents per request.
- Exponential backoff retry on HTTP `429` / `503`.
- Strict 1-hour delay between Anchor downloads to protect VM IP from exchange blocking.
- Validates `%PDF-` file magic headers to reject HTML CAPTCHA challenges before saving.
