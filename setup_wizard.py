#!/usr/bin/env python3
"""
Flask Platform Setup Wizard
============================
Self-contained, one-shot setup wizard for a Flask-based web platform.

Usage:
    python setup_wizard.py [--port PORT] [--host HOST]

The wizard spins up a temporary web interface on the given host/port (default
localhost:5050), guides the admin through initial configuration, provisions the
server, and shuts itself down cleanly once complete.
"""

from __future__ import annotations

import argparse
import json
import multiprocessing
import os
import re
import socket
import subprocess
import sys
import threading
import urllib.request
from queue import Empty, Queue

from flask import Flask, Response, jsonify, redirect, render_template_string, request, session, url_for

# Platform guard: the setup wizard is only supported on Linux

if sys.platform != "linux":
    print(
        "The setup wizard is only supported on Linux.\n"
        "Use the Easy Deployment App on Windows/macOS, or deploy manually."
    )
    raise SystemExit(1)

# App setup

app = Flask(__name__)
app.secret_key = os.urandom(32)

# Global state (single-admin wizard. Not intended for concurrent use)

_config: dict = {}          # Persisted form data across phases
_log_queue: Queue = Queue() # SSE log messages for Phase 3
_provisioning_done = threading.Event()
_non_interactive: bool = False  # Set to True when running without the web UI

# Helper utilities

DEFAULT_WORKERS = 2 * multiprocessing.cpu_count() + 1


def _get_public_ip() -> str:
    """Try several public-IP-detection endpoints; return the first that works."""
    urls = [
        "https://ifconfig.me/ip",
        "https://api.ipify.org",
        "https://ipecho.net/plain",
    ]
    for url in urls:
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:  # noqa: S310
                ip = resp.read().decode().strip()
                if re.match(r"^\d{1,3}(\.\d{1,3}){3}$", ip):
                    return ip
        except Exception:
            continue
    return "UNKNOWN"


def _get_ipv6() -> str:
    """Return the server's public IPv6 address, or empty string."""
    try:
        result = subprocess.run(
            ["curl", "-6", "-s", "--connect-timeout", "4", "https://ifconfig.me/ip"],
            capture_output=True, text=True, timeout=5,
        )
        ip = result.stdout.strip()
        if ":" in ip:
            return ip
    except Exception:
        pass
    return ""


def _check_dns(domain: str, expected_ip: str) -> tuple[bool, str]:
    """Check whether *domain* resolves to *expected_ip*."""
    try:
        results = socket.getaddrinfo(domain, None)
        resolved = {r[4][0] for r in results}
        if expected_ip in resolved:
            return True, f"✓ {domain} → {expected_ip}"
        return False, f"✗ {domain} resolves to {resolved!r}, expected {expected_ip}"
    except socket.gaierror:
        return False, f"✗ DNS lookup failed for {domain}"


def _log(msg: str, level: str = "info") -> None:
    if _non_interactive:
        prefix = {"head": ">>>", "ok": "[OK]", "warn": "[WARN]", "err": "[ERR]"}.get(level, "   ")
        print(f"{prefix} {msg}", flush=True)
    else:
        _log_queue.put({"level": level, "msg": msg})


def _run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    """Run a shell command, streaming output to the log queue."""
    _log(f"$ {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.stdout:
        for line in result.stdout.splitlines():
            _log(line)
    if result.stderr:
        for line in result.stderr.splitlines():
            _log(line, "warn")
    if check and result.returncode != 0:
        raise subprocess.CalledProcessError(result.returncode, cmd)
    return result


# Embedded HTML templates

_BASE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Flask Platform Setup Wizard</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Segoe UI',sans-serif;background:#0f172a;color:#e2e8f0;min-height:100vh}
.container{max-width:1200px;margin:0 auto;padding:2rem 1rem}
h1{font-size:1.8rem;color:#38bdf8;margin-bottom:.25rem}
.subtitle{color:#94a3b8;margin-bottom:2rem;font-size:.95rem}
h2{font-size:1.2rem;color:#7dd3fc;margin-bottom:1rem;border-bottom:1px solid #1e3a5f;padding-bottom:.5rem}
h3{font-size:1rem;color:#94a3b8;margin-bottom:.75rem}
.layout{display:grid;grid-template-columns:1fr 1fr;gap:1.5rem}
@media(max-width:900px){.layout{grid-template-columns:1fr}}
.card{background:#1e293b;border-radius:8px;padding:1.5rem;border:1px solid #334155}
.field{margin-bottom:1.25rem}
label{display:block;font-size:.85rem;color:#94a3b8;margin-bottom:.4rem;font-weight:500}
input[type=text],input[type=number],select,textarea{
  width:100%;padding:.5rem .75rem;background:#0f172a;border:1px solid #334155;
  border-radius:6px;color:#e2e8f0;font-size:.9rem;transition:border-color .2s
}
input:focus,select:focus,textarea:focus{outline:none;border-color:#38bdf8}
input.error{border-color:#f87171}
.hint{font-size:.78rem;color:#64748b;margin-top:.3rem}
.row{display:grid;grid-template-columns:1fr 1fr;gap:1rem}
.checkbox-row{display:flex;align-items:center;gap:.5rem;margin-bottom:1rem}
.checkbox-row input{width:auto}
.checkbox-row label{margin:0;font-size:.9rem;color:#cbd5e1}
.env-row{display:grid;grid-template-columns:1fr 1fr auto;gap:.5rem;margin-bottom:.5rem}
.env-row input{margin:0}
button.add-env{background:#1d4ed8;border:none;color:#fff;padding:.4rem .9rem;
  border-radius:6px;cursor:pointer;font-size:.85rem;margin-top:.25rem}
button.add-env:hover{background:#2563eb}
button.remove-env{background:#7f1d1d;border:none;color:#fff;padding:.25rem .6rem;
  border-radius:6px;cursor:pointer;font-size:.85rem}
.btn-primary{display:inline-block;padding:.65rem 1.75rem;background:#0369a1;
  border:none;color:#fff;border-radius:6px;font-size:.95rem;cursor:pointer;
  font-weight:600;transition:background .2s}
.btn-primary:hover{background:#0284c7}
.btn-secondary{display:inline-block;padding:.65rem 1.75rem;background:#334155;
  border:none;color:#e2e8f0;border-radius:6px;font-size:.95rem;cursor:pointer;font-weight:600}
.btn-success{background:#15803d;color:#fff;border:none;padding:.65rem 1.75rem;
  border-radius:6px;font-size:.95rem;cursor:pointer;font-weight:600}
.btn-success:hover{background:#16a34a}
.btn-danger{background:#991b1b;color:#fff;border:none;padding:.65rem 1.75rem;
  border-radius:6px;font-size:.95rem;cursor:pointer;font-weight:600}
pre.preview{background:#020617;border:1px solid #1e3a5f;border-radius:6px;
  padding:1rem;font-size:.78rem;color:#7dd3fc;overflow-x:auto;white-space:pre;
  min-height:80px;line-height:1.6}
.section-toggle{display:none}
.section-toggle.visible{display:block}
.badge{display:inline-block;padding:.15rem .5rem;border-radius:4px;font-size:.75rem;font-weight:600}
.badge-info{background:#0c4a6e;color:#7dd3fc}
.badge-warn{background:#78350f;color:#fcd34d}
.badge-ok{background:#14532d;color:#86efac}
.badge-err{background:#7f1d1d;color:#fca5a5}
.dns-record{background:#0f172a;border:1px solid #1e3a5f;border-radius:6px;
  padding:.75rem 1rem;font-family:monospace;font-size:.85rem;margin-bottom:.5rem;color:#a5f3fc}
.log-box{background:#020617;border:1px solid #1e3a5f;border-radius:6px;
  padding:1rem;height:400px;overflow-y:auto;font-family:monospace;font-size:.8rem;line-height:1.7}
.log-info{color:#94a3b8}
.log-warn{color:#fbbf24}
.log-err{color:#f87171}
.log-ok{color:#86efac}
.log-head{color:#38bdf8;font-weight:bold}
.step-list{list-style:none;margin-bottom:1.5rem}
.step-list li{padding:.5rem .75rem;margin-bottom:.4rem;border-radius:6px;
  background:#0f172a;border-left:3px solid #334155;font-size:.9rem;color:#94a3b8}
.step-list li.active{border-left-color:#38bdf8;color:#e2e8f0}
.step-list li.done{border-left-color:#22c55e;color:#86efac}
.step-list li.fail{border-left-color:#ef4444;color:#fca5a5}
#dns-status{margin-top:1rem;padding:.75rem 1rem;border-radius:6px;display:none}
.alert{padding:.75rem 1rem;border-radius:6px;margin-bottom:1.5rem;font-size:.9rem}
.alert-info{background:#0c4a6e;border:1px solid #0369a1;color:#bae6fd}
.alert-warn{background:#78350f;border:1px solid #b45309;color:#fde68a}
.alert-success{background:#14532d;border:1px solid #15803d;color:#bbf7d0}
.alert-error{background:#7f1d1d;border:1px solid #991b1b;color:#fecaca}
.tabs{display:flex;gap:.5rem;margin-bottom:1rem}
.tab{padding:.4rem 1rem;border-radius:6px 6px 0 0;background:#0f172a;
  border:1px solid #334155;border-bottom:none;cursor:pointer;font-size:.85rem;color:#94a3b8}
.tab.active{background:#1e293b;color:#38bdf8;border-bottom:1px solid #1e293b}
.tab-panel{display:none}.tab-panel.active{display:block}
</style>
</head>
<body>
<div class="container">
  <h1>⚡ Flask Platform Setup Wizard</h1>
  <p class="subtitle">One-shot provisioning for your Flask-based web platform</p>
  {% block content %}{% endblock %}
</div>
</body>
</html>"""


_FORM_HTML = _BASE_HTML.replace("{% block content %}{% endblock %}", """
<div class="layout">
  <!-- ===== LEFT: FORM ===== -->
  <div>

    <form id="setupForm" method="POST" action="/configure" novalidate>

      <!-- Phase indicator -->
      <div class="alert alert-info" style="margin-bottom:1.5rem">
        <strong>Phase 1 of 3</strong>: Fill in the form below.
        A live configuration preview updates on the right as you type.
      </div>

      <!-- SITE INFO -->
      <div class="card" style="margin-bottom:1.25rem">
        <h2>Site Information</h2>
        <div class="field">
          <label for="site_name">Site / App Name *</label>
          <input type="text" id="site_name" name="site_name"
            placeholder="mywikiapp" value="{{ cfg.site_name or '' }}"
            pattern="[a-z0-9_-]+" required/>
          <p class="hint">Lowercase, letters, numbers, hyphens, underscores only.
            Used as the systemd service name and nginx config filename.</p>
        </div>
        <div class="field">
          <label for="deploy_mode">Deployment Mode *</label>
          <select id="deploy_mode" name="deploy_mode">
            <option value="ip" {% if cfg.deploy_mode != 'domain' %}selected{% endif %}>IP Only</option>
            <option value="domain" {% if cfg.deploy_mode == 'domain' %}selected{% endif %}>Custom Domain</option>
          </select>
        </div>
        <div id="domain_section" class="section-toggle {% if cfg.deploy_mode == 'domain' %}visible{% endif %}">
          <div class="field">
            <label for="domain">Domain Name</label>
            <input type="text" id="domain" name="domain"
              placeholder="example.com" value="{{ cfg.domain or '' }}"/>
            <p class="hint">Just the bare domain, no http:// prefix.</p>
          </div>
          <div class="checkbox-row">
            <input type="checkbox" id="include_www" name="include_www" value="1"
              {% if cfg.include_www %}checked{% endif %}/>
            <label for="include_www">Also serve www.&lt;domain&gt;</label>
          </div>
          <div class="checkbox-row">
            <input type="checkbox" id="redirect_https" name="redirect_https" value="1"
              {% if cfg.redirect_https %}checked{% endif %}/>
            <label for="redirect_https">Redirect HTTP → HTTPS</label>
          </div>
          <div class="checkbox-row">
            <input type="checkbox" id="use_certbot" name="use_certbot" value="1"
              {% if cfg.use_certbot %}checked{% endif %}/>
            <label for="use_certbot">Obtain SSL certificate via Certbot (Let's Encrypt)</label>
          </div>
          <div class="field" id="certbot_email_field" style="{% if not cfg.use_certbot %}display:none{% endif %}">
            <label for="certbot_email">Certbot Notification Email (recommended)</label>
            <input type="text" id="certbot_email" name="certbot_email"
              placeholder="admin@example.com" value="{{ cfg.certbot_email or '' }}"/>
            <p class="hint">Let's Encrypt will send certificate expiry reminders to this address.</p>
          </div>
        </div>
      </div>

      <!-- APP CONFIG -->
      <div class="card" style="margin-bottom:1.25rem">
        <h2>Application Configuration</h2>
        <div class="field">
          <label for="entrypoint">App Entry Point *</label>
          <input type="text" id="entrypoint" name="entrypoint"
            placeholder="app:app" value="{{ cfg.entrypoint or 'app:app' }}" required/>
          <p class="hint">Python module and callable, e.g. <code>wsgi:app</code></p>
        </div>
        <div class="row">
          <div class="field">
            <label for="venv_path">Virtualenv Path *</label>
            <input type="text" id="venv_path" name="venv_path"
              placeholder="/var/www/myapp/venv" value="{{ cfg.venv_path or '' }}" required/>
          </div>
          <div class="field">
            <label for="app_dir">App Working Directory *</label>
            <input type="text" id="app_dir" name="app_dir"
              placeholder="/var/www/myapp" value="{{ cfg.app_dir or '' }}" required/>
          </div>
        </div>
        <div class="row">
          <div class="field">
            <label for="app_user">App User *</label>
            <input type="text" id="app_user" name="app_user"
              placeholder="www-data" value="{{ cfg.app_user or '' }}" required/>
            <p class="hint">Do not use root.</p>
          </div>
          <div class="field">
            <label for="app_group">App Group</label>
            <input type="text" id="app_group" name="app_group"
              placeholder="(defaults to user)" value="{{ cfg.app_group or '' }}"/>
          </div>
        </div>
        <div class="row">
          <div class="field">
            <label for="workers">Gunicorn Workers</label>
            <input type="number" id="workers" name="workers" min="1" max="64"
              placeholder="{{ default_workers }}"
              value="{{ cfg.workers or default_workers }}"/>
            <p class="hint">Recommended: 2 × CPU cores + 1 = {{ default_workers }}</p>
          </div>
          <div class="field">
            <label for="sock_path">Gunicorn Socket Path *</label>
            <input type="text" id="sock_path" name="sock_path"
              placeholder="/run/myapp/myapp.sock"
              value="{{ cfg.sock_path or '' }}" required/>
          </div>
        </div>
        <div class="row">
          <div class="field">
            <label for="http_port">HTTP Port</label>
            <input type="number" id="http_port" name="http_port" min="1" max="65535"
              value="{{ cfg.http_port or 80 }}"/>
          </div>
        </div>
      </div>

      <!-- ENVIRONMENT VARIABLES -->
      <div class="card" style="margin-bottom:1.25rem">
        <h2>Environment Variables</h2>
        <p class="hint" style="margin-bottom:.75rem">
          These will be written to <code>/etc/systemd/system/&lt;appname&gt;.env</code>
          (chmod 600, owned by root).
        </p>
        <div id="env_pairs">
          {% for k, v in (cfg.env_vars or []) %}
          <div class="env-row">
            <input type="text" name="env_key[]" placeholder="KEY" value="{{ k }}"/>
            <input type="text" name="env_val[]" placeholder="value" value="{{ v }}"/>
            <button type="button" class="remove-env" onclick="removeEnv(this)">✕</button>
          </div>
          {% endfor %}
        </div>
        <button type="button" class="add-env" onclick="addEnv()">+ Add Variable</button>
      </div>

      <button type="submit" class="btn-primary">Continue →</button>
    </form>
  </div>

  <!-- ===== RIGHT: PREVIEW ===== -->
  <div>
    <div class="card" style="position:sticky;top:1rem">
      <h2>Live Configuration Preview</h2>
      <div class="tabs">
        <div class="tab active" onclick="showTab('tab-service', event)">systemd service</div>
        <div class="tab" onclick="showTab('tab-env', event)">env file</div>
        <div class="tab" onclick="showTab('tab-nginx', event)">nginx</div>
      </div>
      <div id="tab-service" class="tab-panel active">
        <pre class="preview" id="prev_service"></pre>
      </div>
      <div id="tab-env" class="tab-panel">
        <pre class="preview" id="prev_env"></pre>
      </div>
      <div id="tab-nginx" class="tab-panel">
        <pre class="preview" id="prev_nginx"></pre>
      </div>
    </div>
  </div>
</div>

<script>
// ---- Tab switching ----
function showTab(id, evt) {
  document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.getElementById(id).classList.add('active');
  if (evt && evt.target) evt.target.classList.add('active');
}

// ---- Deploy mode toggle ----
document.getElementById('deploy_mode').addEventListener('change', function() {
  const ds = document.getElementById('domain_section');
  ds.classList.toggle('visible', this.value === 'domain');
  updatePreview();
});

document.getElementById('use_certbot').addEventListener('change', function() {
  const ef = document.getElementById('certbot_email_field');
  if (ef) ef.style.display = this.checked ? '' : 'none';
});

// ---- Env variable management ----
function addEnv() {
  const container = document.getElementById('env_pairs');
  const row = document.createElement('div');
  row.className = 'env-row';
  row.innerHTML = '<input type="text" name="env_key[]" placeholder="KEY"/>' +
                  '<input type="text" name="env_val[]" placeholder="value"/>' +
                  '<button type="button" class="remove-env" onclick="removeEnv(this)">✕</button>';
  container.appendChild(row);
  row.querySelectorAll('input').forEach(i => i.addEventListener('input', updatePreview));
}
function removeEnv(btn) { btn.closest('.env-row').remove(); updatePreview(); }

// ---- Live preview generator ----
function getVal(id) { const el = document.getElementById(id); return el ? el.value.trim() : ''; }
function getChecked(id) { const el = document.getElementById(id); return el ? el.checked : false; }

function buildServicePreview(cfg) {
  const env = cfg.envVars.map(([k]) => `Environment="${k}=..."`).join('\n');
  return `[Unit]
Description=${cfg.siteName} gunicorn service
After=network.target

[Service]
Type=notify
User=${cfg.user || '<user>'}
Group=${cfg.group || cfg.user || '<group>'}
WorkingDirectory=${cfg.appDir || '<app_dir>'}
RuntimeDirectory=${cfg.siteName || '<appname>'}
EnvironmentFile=/etc/systemd/system/${cfg.siteName || '<appname>'}.env
ExecStart=${cfg.venv || '<venv>'}/bin/gunicorn \\
    --workers ${cfg.workers || 3} \\
    --bind unix:${cfg.sock || '<sock_path>'} \\
    ${cfg.entry || '<entrypoint>'}
ExecReload=/bin/kill -s HUP $MAINPID
KillMode=mixed
TimeoutStopSec=5
PrivateTmp=true
Restart=on-failure

[Install]
WantedBy=multi-user.target`;
}

function buildEnvPreview(cfg) {
  if (!cfg.envVars.length) return '# No environment variables defined';
  return cfg.envVars.map(([k, v]) => `${k}=${v}`).join('\n');
}

function buildNginxPreview(cfg) {
  const name = cfg.siteName || '<appname>';
  const sock = cfg.sock || '<sock_path>';
  const port = cfg.httpPort || 80;
  let serverName = '_';
  if (cfg.mode === 'domain' && cfg.domain) {
    serverName = cfg.domain + (cfg.www ? ' www.' + cfg.domain : '');
  }
  let upstreamBlock = `upstream ${name}_app {
    server unix:${sock} fail_timeout=0;
}`;
  let httpsBlock = '';
  if (cfg.mode === 'domain' && cfg.redirectHttps) {
    httpsBlock = `
server {
    listen ${port};
    server_name ${serverName};
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl;
    server_name ${serverName};
    ssl_certificate /etc/letsencrypt/live/${cfg.domain}/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/${cfg.domain}/privkey.pem;
    include /etc/letsencrypt/options-ssl-nginx.conf;

    location / {
        proxy_pass http://${name}_app;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}`;
  } else {
    httpsBlock = `
server {
    listen ${port};
    server_name ${serverName};

    location / {
        proxy_pass http://${name}_app;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}`;
  }
  return upstreamBlock + '\n' + httpsBlock;
}

function getEnvVars() {
  const keys = [...document.querySelectorAll('input[name="env_key[]"]')].map(e => e.value.trim());
  const vals = [...document.querySelectorAll('input[name="env_val[]"]')].map(e => e.value.trim());
  return keys.map((k, i) => [k, vals[i] || '']).filter(([k]) => k);
}

function updatePreview() {
  const cfg = {
    siteName: getVal('site_name'),
    mode: getVal('deploy_mode'),
    domain: getVal('domain'),
    www: getChecked('include_www'),
    redirectHttps: getChecked('redirect_https'),
    entry: getVal('entrypoint'),
    venv: getVal('venv_path'),
    appDir: getVal('app_dir'),
    user: getVal('app_user'),
    group: getVal('app_group'),
    workers: parseInt(getVal('workers')) || {{ default_workers }},
    sock: getVal('sock_path'),
    httpPort: getVal('http_port') || 80,
    envVars: getEnvVars(),
  };
  document.getElementById('prev_service').textContent = buildServicePreview(cfg);
  document.getElementById('prev_env').textContent = buildEnvPreview(cfg);
  document.getElementById('prev_nginx').textContent = buildNginxPreview(cfg);
}

// ---- Client-side validation ----
document.getElementById('setupForm').addEventListener('submit', function(e) {
  let valid = true;
  const required = ['site_name','entrypoint','venv_path','app_dir','app_user','sock_path'];
  required.forEach(id => {
    const el = document.getElementById(id);
    if (!el) return;
    el.classList.remove('error');
    if (!el.value.trim()) { el.classList.add('error'); valid = false; }
  });
  if (document.getElementById('deploy_mode').value === 'domain') {
    const d = document.getElementById('domain');
    d.classList.remove('error');
    const domainVal = d.value.trim();
    const domainRe = /^([a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\\.)+[a-zA-Z]{2,}$/;
    if (!domainVal || !domainRe.test(domainVal)) { d.classList.add('error'); valid = false; }
  }
  // Validate site_name format
  const sn = document.getElementById('site_name');
  if (sn.value.trim() && !/^[a-z0-9_-]+$/.test(sn.value.trim())) {
    sn.classList.add('error'); valid = false;
  }
  // Validate paths start with /
  ['venv_path','app_dir','sock_path'].forEach(id => {
    const el = document.getElementById(id);
    if (el && el.value.trim() && !el.value.trim().startsWith('/')) {
      el.classList.add('error'); valid = false;
    }
  });
  if (!valid) { e.preventDefault(); alert('Please fix the highlighted fields before continuing.'); }
});

// ---- Attach input listeners ----
document.querySelectorAll('input,select,textarea').forEach(el => {
  el.addEventListener('input', updatePreview);
  el.addEventListener('change', updatePreview);
});

// Initial render
updatePreview();
</script>
""")

_DNS_HTML = _BASE_HTML.replace("{% block content %}{% endblock %}", """
<div style="max-width:700px">
  <div class="alert alert-warn">
    <strong>Phase 2: DNS Configuration Required</strong><br/>
    Before provisioning can begin, you need to set the following DNS records.
    DNS verification must pass before continuing.
  </div>

  <div class="card" style="margin-bottom:1.25rem">
    <h2>Required DNS Records</h2>
    <p style="color:#94a3b8;font-size:.9rem;margin-bottom:1rem">
      Set these records with your domain registrar or DNS provider.
      Changes may take a few minutes to propagate.
    </p>

    <div class="dns-record">
      <span class="badge badge-info">A</span>
      &nbsp; <strong>{{ domain }}</strong> → {{ server_ip }}
    </div>
    {% if include_www %}
    <div class="dns-record">
      <span class="badge badge-info">A</span>
      &nbsp; <strong>www.{{ domain }}</strong> → {{ server_ip }}
    </div>
    {% endif %}
    {% if ipv6 %}
    <div class="dns-record">
      <span class="badge badge-warn">AAAA</span>
      &nbsp; <strong>{{ domain }}</strong> → {{ ipv6 }}
    </div>
    {% if include_www %}
    <div class="dns-record">
      <span class="badge badge-warn">AAAA</span>
      &nbsp; <strong>www.{{ domain }}</strong> → {{ ipv6 }}
    </div>
    {% endif %}
    {% endif %}
  </div>

  <div class="card" style="margin-bottom:1.25rem">
    <h2>DNS Verification</h2>
    <p style="color:#94a3b8;font-size:.9rem;margin-bottom:1rem">
      Click <strong>Verify DNS</strong> to check whether your domain resolves correctly.
      You can re-check as many times as needed.
    </p>
    <button class="btn-secondary" id="verifyBtn" onclick="verifyDNS()">🔍 Verify DNS</button>
    <div id="dns-status" style="margin-top:1rem;padding:.75rem 1rem;border-radius:6px;display:none"></div>
  </div>

  <form method="POST" action="/provision">
    <button type="submit" class="btn-primary" id="continueBtn" disabled
      style="opacity:.5;cursor:not-allowed">
      Proceed to Provisioning →
    </button>
    <span id="continueHint" style="color:#64748b;font-size:.85rem;margin-left:.75rem">
      (DNS must be verified first)
    </span>
  </form>
</div>

<script>
let dnsOk = false;

async function verifyDNS() {
  const btn = document.getElementById('verifyBtn');
  const status = document.getElementById('dns-status');
  btn.disabled = true;
  btn.textContent = '⏳ Checking…';
  status.style.display = 'none';

  try {
    const resp = await fetch('/verify_dns', {method: 'POST'});
    const data = await resp.json();
    status.style.display = 'block';
    if (data.ok) {
      status.className = 'alert alert-success';
      status.innerHTML = '<strong>✓ DNS verified!</strong><br/>' +
        data.results.map(r => r.msg).join('<br/>');
      dnsOk = true;
      const continueBtn = document.getElementById('continueBtn');
      continueBtn.disabled = false;
      continueBtn.style.opacity = '1';
      continueBtn.style.cursor = 'pointer';
      document.getElementById('continueHint').style.display = 'none';
    } else {
      status.className = 'alert alert-error';
      status.innerHTML = '<strong>✗ DNS not yet ready</strong><br/>' +
        data.results.map(r => r.msg).join('<br/>');
    }
  } catch(err) {
    status.style.display = 'block';
    status.className = 'alert alert-error';
    status.textContent = 'Error checking DNS: ' + err;
  }
  btn.disabled = false;
  btn.textContent = '🔍 Verify DNS';
}
</script>
""")

_PROVISION_HTML = _BASE_HTML.replace("{% block content %}{% endblock %}", """
<div class="layout">
  <div>
    <div class="alert alert-info" style="margin-bottom:1.5rem">
      <strong>Phase 3: Automated Provisioning</strong><br/>
      The wizard is now setting up your server. Do not close this window.
    </div>

    <div class="card">
      <h2>Provisioning Steps</h2>
      <ul class="step-list" id="stepList">
        <li id="step-packages">Install system packages</li>
        <li id="step-sockdir">Create socket directory</li>
        <li id="step-envfile">Write environment file</li>
        <li id="step-service">Write systemd service</li>
        <li id="step-systemd">Enable & start service</li>
        <li id="step-nginx">Write nginx config</li>
        <li id="step-certbot">Obtain SSL certificate</li>
        <li id="step-done">Finalise & self-destruct</li>
      </ul>
    </div>
  </div>
  <div>
    <div class="card">
      <h2>Live Log</h2>
      <div class="log-box" id="logBox"></div>
      <div id="provision-result" style="margin-top:1rem;display:none"></div>
    </div>
  </div>
</div>

<script>
const logBox = document.getElementById('logBox');
const stepMap = {
  'packages': 'step-packages',
  'sockdir':  'step-sockdir',
  'envfile':  'step-envfile',
  'service':  'step-service',
  'systemd':  'step-systemd',
  'nginx':    'step-nginx',
  'certbot':  'step-certbot',
  'done':     'step-done',
};

function appendLog(msg, level) {
  const line = document.createElement('div');
  line.className = 'log-' + (level || 'info');
  line.textContent = msg;
  logBox.appendChild(line);
  logBox.scrollTop = logBox.scrollHeight;
}

function setStep(step, state) {
  const el = document.getElementById(stepMap[step]);
  if (el) { el.className = state; }
}

const es = new EventSource('/log_stream');
es.addEventListener('log', function(e) {
  const data = JSON.parse(e.data);
  appendLog(data.msg, data.level);
});
es.addEventListener('step', function(e) {
  const data = JSON.parse(e.data);
  setStep(data.step, data.state);
});
es.addEventListener('done', function(e) {
  const data = JSON.parse(e.data);
  es.close();
  const result = document.getElementById('provision-result');
  result.style.display = 'block';
  if (data.success) {
    result.innerHTML = '<div class="alert alert-success"><strong>✓ Provisioning complete!</strong><br/>' +
      data.message + '<br/><br/><em>This setup wizard will shut down in 5 seconds…</em></div>';
    setTimeout(() => { window.location.href = '/done'; }, 5000);
  } else {
    result.innerHTML = '<div class="alert alert-error"><strong>✗ Provisioning failed</strong><br/>' +
      data.message + '</div>';
  }
});
es.onerror = function() {
  if (es.readyState === EventSource.CLOSED) return;
  appendLog('Connection lost. Reload the page to reconnect.', 'err');
};
</script>
""")

_DONE_HTML = _BASE_HTML.replace("{% block content %}{% endblock %}", """
<div style="max-width:600px;text-align:center;margin:4rem auto">
  <div style="font-size:4rem;margin-bottom:1rem">🎉</div>
  <h2 style="color:#86efac;font-size:1.6rem;margin-bottom:1rem">Setup Complete!</h2>
  <p style="color:#94a3b8;margin-bottom:2rem">
    Your Flask platform has been provisioned and the setup wizard is shutting down.<br/>
    You can now access your application at:
  </p>
  <div class="dns-record" style="font-size:1rem;text-align:center;margin-bottom:2rem">
    {{ url }}
  </div>
  <p style="color:#64748b;font-size:.85rem">
    This page will go away once the wizard process exits.
  </p>
</div>
""")

# Routes: Phase 1 (form)

@app.route("/", methods=["GET"])
def index():
    cfg = session.get("cfg", {})
    return render_template_string(
        _FORM_HTML,
        cfg=cfg,
        default_workers=DEFAULT_WORKERS,
    )


@app.route("/configure", methods=["POST"])
def configure():
    """Persist form data and advance to Phase 2 (DNS) or Phase 3 (IP)."""
    env_keys = request.form.getlist("env_key[]")
    env_vals = request.form.getlist("env_val[]")
    env_vars = [(k.strip(), v.strip()) for k, v in zip(env_keys, env_vals, strict=False) if k.strip()]

    cfg = {
        "site_name":      request.form.get("site_name", "").strip(),
        "deploy_mode":    request.form.get("deploy_mode", "ip"),
        "domain":         request.form.get("domain", "").strip(),
        "include_www":    bool(request.form.get("include_www")),
        "redirect_https": bool(request.form.get("redirect_https")),
        "use_certbot":    bool(request.form.get("use_certbot")),
        "certbot_email":  request.form.get("certbot_email", "").strip(),
        "entrypoint":     request.form.get("entrypoint", "app:app").strip(),
        "venv_path":      request.form.get("venv_path", "").strip(),
        "app_dir":        request.form.get("app_dir", "").strip(),
        "app_user":       request.form.get("app_user", "").strip(),
        "app_group":      request.form.get("app_group", "").strip(),
        "workers":        int(request.form.get("workers") or DEFAULT_WORKERS),
        "sock_path":      request.form.get("sock_path", "").strip(),
        "http_port":      int(request.form.get("http_port") or 80),
        "env_vars":       env_vars,
    }
    if not cfg["app_group"]:
        cfg["app_group"] = cfg["app_user"]

    session["cfg"] = cfg

    if cfg["deploy_mode"] == "domain":
        return redirect(url_for("dns_page"))
    return redirect(url_for("provision_page"))


# Routes: Phase 2 (DNS)

@app.route("/dns", methods=["GET"])
def dns_page():
    cfg = session.get("cfg", {})
    if not cfg:
        return redirect(url_for("index"))
    server_ip = _get_public_ip()
    session["server_ip"] = server_ip
    ipv6 = _get_ipv6()
    return render_template_string(
        _DNS_HTML,
        domain=cfg.get("domain", ""),
        include_www=cfg.get("include_www", False),
        server_ip=server_ip,
        ipv6=ipv6,
    )


@app.route("/verify_dns", methods=["POST"])
def verify_dns():
    cfg = session.get("cfg", {})
    server_ip = session.get("server_ip", _get_public_ip())
    domain = cfg.get("domain", "")
    results = []
    all_ok = True

    ok, msg = _check_dns(domain, server_ip)
    results.append({"ok": ok, "msg": msg})
    if not ok:
        all_ok = False

    if cfg.get("include_www"):
        ok2, msg2 = _check_dns(f"www.{domain}", server_ip)
        results.append({"ok": ok2, "msg": msg2})
        if not ok2:
            all_ok = False

    if all_ok:
        session["dns_verified"] = True

    return jsonify({"ok": all_ok, "results": results})


# Routes: Phase 3 (provisioning)

@app.route("/provision", methods=["GET", "POST"])
def provision_page():
    cfg = session.get("cfg", {})
    if not cfg:
        return redirect(url_for("index"))
    if cfg.get("deploy_mode") == "domain" and not session.get("dns_verified"):
        return redirect(url_for("dns_page"))

    # Kick off provisioning in background thread (only once)
    if not _provisioning_done.is_set():
        t = threading.Thread(target=_run_provisioning, args=(dict(cfg),), daemon=True)
        t.start()

    return render_template_string(_PROVISION_HTML)


@app.route("/log_stream")
def log_stream():
    """SSE endpoint streaming provisioning log messages."""
    def generate():
        while not _provisioning_done.is_set():
            try:
                item = _log_queue.get(timeout=1)
                if item.get("__step__"):
                    yield f"event: step\ndata: {json.dumps(item)}\n\n"
                elif item.get("__done__"):
                    yield f"event: done\ndata: {json.dumps(item)}\n\n"
                    return
                else:
                    yield f"event: log\ndata: {json.dumps(item)}\n\n"
            except Empty:
                yield ": heartbeat\n\n"
        # Drain remaining messages
        while True:
            try:
                item = _log_queue.get_nowait()
                if item.get("__step__"):
                    yield f"event: step\ndata: {json.dumps(item)}\n\n"
                elif item.get("__done__"):
                    yield f"event: done\ndata: {json.dumps(item)}\n\n"
                    return
                else:
                    yield f"event: log\ndata: {json.dumps(item)}\n\n"
            except Empty:
                break

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/done")
def done_page():
    cfg = session.get("cfg", {})
    if cfg.get("deploy_mode") == "domain" and cfg.get("domain"):
        scheme = "https" if cfg.get("use_certbot") or cfg.get("redirect_https") else "http"
        url = f"{scheme}://{cfg['domain']}"
    else:
        url = "http://<your-server-ip>"
    resp = render_template_string(_DONE_HTML, url=url)
    # Schedule shutdown after response is sent
    threading.Timer(2.0, _shutdown).start()
    return resp


# Provisioning logic

def _step(step: str, state: str) -> None:
    if _non_interactive:
        if state == "active":
            print(f"\n[STEP] {step} ...", flush=True)
        elif state == "done":
            print(f"[STEP] {step} done.", flush=True)
        elif state == "fail":
            print(f"[STEP] {step} FAILED.", flush=True)
    else:
        _log_queue.put({"__step__": True, "step": step, "state": state})


def _run_provisioning(cfg: dict) -> None:
    """Execute all provisioning steps sequentially."""
    name = cfg["site_name"]
    user = cfg["app_user"]
    group = cfg["app_group"]
    venv = cfg["venv_path"]
    app_dir = cfg["app_dir"]
    workers = cfg["workers"]
    sock = cfg["sock_path"]
    entry = cfg["entrypoint"]
    env_vars = cfg.get("env_vars", [])
    domain = cfg.get("domain", "")
    include_www = cfg.get("include_www", False)
    redirect_https = cfg.get("redirect_https", False)
    use_certbot = cfg.get("use_certbot", False)
    certbot_email = cfg.get("certbot_email", "")
    deploy_mode = cfg.get("deploy_mode", "ip")
    http_port = cfg.get("http_port", 80)

    try:
        # Step 1: System packages
        _step("packages", "active")
        _log("=== Installing system packages ===", "head")
        packages = ["nginx", "python3-pip", "gunicorn"]
        if use_certbot:
            packages += ["certbot", "python3-certbot-nginx"]
        _run(["apt-get", "update", "-qq"])
        _run(["apt-get", "install", "-y"] + packages)
        _step("packages", "done")

        # Step 2: Socket directory
        _step("sockdir", "active")
        _log("=== Creating socket directory ===", "head")
        sock_dir = os.path.dirname(sock)
        os.makedirs(sock_dir, exist_ok=True)
        _run(["chown", f"{user}:{group}", sock_dir])
        _step("sockdir", "done")

        # Step 3: Environment file
        _step("envfile", "active")
        _log("=== Writing environment file ===", "head")
        env_file = f"/etc/systemd/system/{name}.env"
        env_content = "\n".join(f"{k}={v}" for k, v in env_vars) + "\n"
        _write_root_file(env_file, env_content, mode=0o600)
        _log(f"Written: {env_file}")
        _step("envfile", "done")

        # Step 4: systemd service
        _step("service", "active")
        _log("=== Writing systemd service file ===", "head")
        service_content = f"""[Unit]
Description={name} gunicorn service
After=network.target

[Service]
Type=notify
User={user}
Group={group}
WorkingDirectory={app_dir}
RuntimeDirectory={name}
EnvironmentFile={env_file}
ExecStart={venv}/bin/gunicorn \\
    --workers {workers} \\
    --bind unix:{sock} \\
    {entry}
ExecReload=/bin/kill -s HUP $MAINPID
KillMode=mixed
TimeoutStopSec=5
PrivateTmp=true
Restart=on-failure

[Install]
WantedBy=multi-user.target
"""
        service_file = f"/etc/systemd/system/{name}.service"
        _write_root_file(service_file, service_content)
        _log(f"Written: {service_file}")
        _step("service", "done")

        # Step 5: systemd enable & start
        _step("systemd", "active")
        _log("=== Enabling and starting systemd service ===", "head")
        _run(["systemctl", "daemon-reload"])
        _run(["systemctl", "enable", name])
        _run(["systemctl", "start", name])
        _step("systemd", "done")

        # Step 6: nginx config
        _step("nginx", "active")
        _log("=== Writing nginx configuration ===", "head")
        nginx_config = _build_nginx_config(
            name=name,
            sock=sock,
            domain=domain,
            include_www=include_www,
            redirect_https=redirect_https,
            use_certbot=use_certbot,
            deploy_mode=deploy_mode,
            http_port=http_port,
        )
        nginx_file = f"/etc/nginx/sites-available/{name}"
        _write_root_file(nginx_file, nginx_config)
        enabled_link = f"/etc/nginx/sites-enabled/{name}"
        if not os.path.exists(enabled_link):
            os.symlink(nginx_file, enabled_link)
        _run(["nginx", "-t"])
        _run(["systemctl", "reload", "nginx"])
        _step("nginx", "done")

        # Step 7: Certbot (optional)
        _step("certbot", "active")
        if use_certbot and deploy_mode == "domain" and domain:
            _log("=== Obtaining SSL certificate via Certbot ===", "head")
            certbot_cmd = ["certbot", "--nginx", "--non-interactive", "--agree-tos"]
            if certbot_email:
                certbot_cmd += ["--email", certbot_email]
            else:
                certbot_cmd += ["--register-unsafely-without-email"]
            certbot_cmd += ["-d", domain]
            if include_www:
                certbot_cmd += ["-d", f"www.{domain}"]
            _run(certbot_cmd)
            _step("certbot", "done")
        else:
            _log("Certbot not requested: skipping.")
            _step("certbot", "done")

        # Step 8: Done
        _step("done", "active")
        _log("=== Provisioning complete! ===", "ok")
        _step("done", "done")
        _provisioning_done.set()
        _log_queue.put({
            "__done__": True,
            "success": True,
            "message": f"Service <strong>{name}</strong> is running. "
                       f"Nginx has been configured and reloaded.",
        })

    except Exception as exc:  # noqa: BLE001
        _log(f"FATAL: {exc}", "err")
        _provisioning_done.set()
        if _non_interactive:
            sys.exit(1)
        _log_queue.put({
            "__done__": True,
            "success": False,
            "message": str(exc),
        })


def _build_nginx_config(
    name: str,
    sock: str,
    domain: str,
    include_www: bool,
    redirect_https: bool,
    use_certbot: bool,
    deploy_mode: str,
    http_port: int,
) -> str:
    if deploy_mode == "domain" and domain:
        server_name = domain + (" www." + domain if include_www else "")
    else:
        server_name = "_"

    upstream = f"""upstream {name}_app {{
    server unix:{sock} fail_timeout=0;
}}
"""
    if deploy_mode == "domain" and (redirect_https or use_certbot):
        # HTTP → HTTPS redirect + HTTPS server block (certbot will fill in SSL paths)
        server_blocks = f"""
server {{
    listen {http_port};
    server_name {server_name};
    return 301 https://$host$request_uri;
}}

server {{
    listen 443 ssl;
    server_name {server_name};
    # SSL managed by Certbot

    location / {{
        proxy_pass http://{name}_app;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_redirect off;
        proxy_buffering off;
    }}
}}
"""
    else:
        server_blocks = f"""
server {{
    listen {http_port};
    server_name {server_name};

    location / {{
        proxy_pass http://{name}_app;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_redirect off;
        proxy_buffering off;
    }}
}}
"""
    return upstream + server_blocks


def _write_root_file(path: str, content: str, mode: int = 0o640) -> None:
    """Write *content* to *path* creating parent dirs as needed, then chmod.

    Only paths within allowed system directories are accepted to prevent
    accidental writes to unintended locations.
    """
    _ALLOWED_PREFIXES = (
        "/etc/systemd/system/",
        "/etc/nginx/",
        "/run/",
    )
    real = os.path.realpath(os.path.abspath(path))
    if not any(real.startswith(p) for p in _ALLOWED_PREFIXES):
        raise ValueError(
            f"Refusing to write to '{real}': not under an allowed system directory "
            f"({', '.join(_ALLOWED_PREFIXES)})"
        )
    os.makedirs(os.path.dirname(real), exist_ok=True)
    with open(real, "w", encoding="utf-8") as fh:
        fh.write(content)
    os.chmod(real, mode)


# Graceful shutdown

def _shutdown() -> None:
    """Terminate this process gracefully."""
    os.kill(os.getpid(), 15)  # SIGTERM


# Entry point

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Flask Platform Setup Wizard")
    parser.add_argument("--port", type=int, default=5050,
                        help="Port to run the setup wizard on (default: 5050)")
    parser.add_argument("--host", default="127.0.0.1",
                        help="Host to bind to (default: 127.0.0.1)")

    # Non-interactive mode: skip the web UI and provision directly from CLI args
    parser.add_argument("--non-interactive", action="store_true",
                        help="Run provisioning without the web UI (all options required)")

    # Required provisioning options
    parser.add_argument("--site-name",
                        help="Site/app name (lowercase, letters/digits/hyphens/underscores)")
    parser.add_argument("--entrypoint", default="wsgi:app",
                        help="Gunicorn entrypoint, e.g. wsgi:app (default: wsgi:app)")
    parser.add_argument("--venv-path",
                        help="Absolute path to the virtualenv, e.g. /var/www/myapp/venv")
    parser.add_argument("--app-dir",
                        help="Absolute path to the app working directory")
    parser.add_argument("--app-user",
                        help="System user that runs the app (do not use root)")
    parser.add_argument("--sock-path",
                        help="Absolute path for the Gunicorn UNIX socket")

    # Optional provisioning options
    parser.add_argument("--app-group",
                        help="System group for the app (defaults to --app-user)")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                        help=f"Gunicorn worker count (default: {DEFAULT_WORKERS})")
    parser.add_argument("--http-port", type=int, default=80,
                        help="HTTP port for nginx (default: 80)")
    parser.add_argument("--deploy-mode", choices=["ip", "domain"], default="ip",
                        help="Deployment mode: ip or domain (default: ip)")
    parser.add_argument("--domain",
                        help="Domain name (required when --deploy-mode=domain)")
    parser.add_argument("--include-www", action="store_true",
                        help="Also serve www.<domain>")
    parser.add_argument("--redirect-https", action="store_true",
                        help="Redirect HTTP to HTTPS")
    parser.add_argument("--use-certbot", action="store_true",
                        help="Obtain SSL certificate via Certbot (Let's Encrypt)")
    parser.add_argument("--certbot-email", default="",
                        help="Email for Certbot/Let's Encrypt notifications")
    parser.add_argument("--env", metavar="KEY=VALUE", action="append", default=[],
                        help="Environment variable to write to the env file "
                             "(can be repeated, e.g. --env SECRET_KEY=abc)")

    args = parser.parse_args()

    if args.non_interactive:
        # Validate required arguments
        required = {
            "--site-name": args.site_name,
            "--venv-path": args.venv_path,
            "--app-dir": args.app_dir,
            "--app-user": args.app_user,
            "--sock-path": args.sock_path,
        }
        missing = [flag for flag, val in required.items() if not val]
        if missing:
            parser.error(
                f"--non-interactive requires: {', '.join(missing)}"
            )
        if args.deploy_mode == "domain" and not args.domain:
            parser.error("--non-interactive with --deploy-mode=domain requires --domain")

        # Parse --env KEY=VALUE pairs
        env_vars = []
        for pair in args.env:
            if "=" not in pair:
                parser.error(f"--env value must be KEY=VALUE, got: {pair!r}")
            k, _, v = pair.partition("=")
            env_vars.append((k.strip(), v.strip()))

        cfg = {
            "site_name":      args.site_name,
            "deploy_mode":    args.deploy_mode,
            "domain":         args.domain or "",
            "include_www":    args.include_www,
            "redirect_https": args.redirect_https,
            "use_certbot":    args.use_certbot,
            "certbot_email":  args.certbot_email,
            "entrypoint":     args.entrypoint,
            "venv_path":      args.venv_path,
            "app_dir":        args.app_dir,
            "app_user":       args.app_user,
            "app_group":      args.app_group or args.app_user,
            "workers":        args.workers,
            "sock_path":      args.sock_path,
            "http_port":      args.http_port,
            "env_vars":       env_vars,
        }

        _non_interactive = True

        print(f"\n{'='*60}")
        print("  Flask Platform Setup: Non-Interactive Provisioning")
        print(f"  Site: {cfg['site_name']}  |  Mode: {cfg['deploy_mode']}")
        print(f"{'='*60}\n")

        _run_provisioning(cfg)

        if not _provisioning_done.is_set():
            sys.exit(1)
    else:
        print(f"\n{'='*60}")
        print("  Flask Platform Setup Wizard")
        print(f"  Open http://{args.host}:{args.port} in your browser")
        print(f"{'='*60}\n")

        app.run(host=args.host, port=args.port, debug=False, threaded=True)
