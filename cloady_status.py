#!/usr/bin/env python3
"""Dependency-free read-only container/host status service."""
from __future__ import annotations
import datetime as dt
import html
import ipaddress
import json
import math
import os
import platform
import re
import selectors
import shutil
import socket
import subprocess
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CACHE_SECONDS = 30
AUX_CACHE_SECONDS = 300
_lock = threading.Lock()
_cache = {"at": 0.0, "data": None}
_aux_lock = threading.Lock()
_aux = {"at": 0.0, "data": None}
CLI_SPECS = {
    "curl": ("--version", r"^curl\s+([0-9][0-9A-Za-z.+_-]*)"),
    "wget": ("--version", r"^GNU Wget\s+([0-9][0-9A-Za-z.+_-]*)"),
    "git": ("--version", r"^git version\s+([0-9][0-9A-Za-z.+_-]*)"),
    "tar": ("--version", r"^tar \(GNU tar\)\s+([0-9][0-9A-Za-z.+_-]*)"),
    "gzip": ("--version", r"^gzip\s+([0-9][0-9A-Za-z.+_-]*)"),
    "unzip": ("-v", r"^UnZip\s+([0-9][0-9A-Za-z.+_-]*)"),
    "ssh": ("-V", r"^OpenSSH_([0-9][0-9A-Za-z.+_-]*)"),
    "openssl": ("version", r"^OpenSSL\s+([0-9][0-9A-Za-z.+_-]*)"),
    "make": ("--version", r"^GNU Make\s+([0-9][0-9A-Za-z.+_-]*)"),
    "bash": ("--version", r"^GNU bash, version\s+([0-9][0-9A-Za-z.+_-]*)"),
}
RUNTIME_SPECS = {
    "Python 3": ("python3", ("--version",)), "Node.js": ("node", ("--version",)),
    "Go": ("go", ("version",)), "Java": ("java", ("-version",)),
    "PHP": ("php", ("--version",)), "Ruby": ("ruby", ("--version",)),
    ".NET": ("dotnet", ("--version",)), "Rust": ("rustc", ("--version",)),
}
_FIXED_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
_RUNTIME_VERSION_PATTERNS = {
    "Python 3": r"^Python\s+([0-9]+(?:\.[0-9A-Za-z+_-]+){1,3})",
    "Node.js": r"^v?([0-9]+(?:\.[0-9A-Za-z+_-]+){1,3})$",
    "Go": r"^go version\s+(go[0-9]+(?:\.[0-9A-Za-z+_-]+){1,3})",
    "Java": r'^openjdk version\s+"([^"\s]{1,40})"|^java version\s+"([^"\s]{1,40})"',
    "PHP": r"^PHP\s+([0-9]+(?:\.[0-9A-Za-z+_-]+){1,3})",
    "Ruby": r"^ruby\s+([0-9]+(?:\.[0-9A-Za-z+_-]+){1,3})",
    ".NET": r"^([0-9]+(?:\.[0-9A-Za-z+_-]+){1,3})$",
    "Rust": r"^rustc\s+([0-9]+(?:\.[0-9A-Za-z+_-]+){1,3})",
}

def read_text(path, limit=65536):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read(limit).strip()
    except (OSError, ValueError): return None

def _safe_version(name, spec):
    exe = shutil.which(name, path=_FIXED_PATH)
    if not exe: return {"installed": False, "version": None, "status": "missing"}
    proc = None
    try:
        proc = subprocess.Popen([exe, *spec], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, shell=False, env={"PATH": _FIXED_PATH, "LANG": "C", "LC_ALL": "C"})
        output = bytearray()
        deadline = time.monotonic() + 2
        with selectors.DefaultSelector() as selector:
            selector.register(proc.stdout, selectors.EVENT_READ)
            while len(output) < 8192 and selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0: raise subprocess.TimeoutExpired([exe, *spec], 2)
                if not selector.select(remaining): raise subprocess.TimeoutExpired([exe, *spec], 2)
                chunk = os.read(proc.stdout.fileno(), min(2048, 8192 - len(output)))
                if not chunk:
                    selector.unregister(proc.stdout)
                    break
                output.extend(chunk)
            if len(output) >= 8192:
                proc.kill()
            remaining = deadline - time.monotonic()
            if remaining <= 0: raise subprocess.TimeoutExpired([exe, *spec], 2)
            proc.wait(timeout=remaining)
        output = output.decode("utf-8", "replace")
        version = None
        runtime_label = next((label for label, (binary, _) in RUNTIME_SPECS.items() if binary == name), None)
        pattern = CLI_SPECS[name][1] if name in CLI_SPECS else _RUNTIME_VERSION_PATTERNS.get(runtime_label)
        for line in output.splitlines()[:12]:
            match = re.match(pattern, line.strip(), re.I) if pattern else None
            if match:
                version = next((g for g in match.groups() if g), "")[:40]; break
        return {"installed": True, "version": version, "status": "identified" if version else "unknown"}
    except (OSError, subprocess.TimeoutExpired):
        return {"installed": True, "version": None, "status": "unknown"}
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
            try: proc.wait(timeout=1)
            except subprocess.TimeoutExpired: pass
        if proc is not None and proc.stdout is not None:
            proc.stdout.close()

def _auxiliary():
    with _aux_lock:
        if _aux["data"] is None or time.monotonic() - _aux["at"] >= AUX_CACHE_SECONDS:
            runtime_info = {name: _safe_version(exe, args) for name, (exe, args) in RUNTIME_SPECS.items()}
            prefixes = {"Python 3":"Python ", "Node.js":"v", "Go":"go version ",
                        "Java":"openjdk version ", "PHP":"PHP ", "Ruby":"ruby ",
                        "Rust":"rustc "}
            runtimes = {name: ((prefixes.get(name, "") + item["version"]) if item["version"] else None)
                        for name, item in runtime_info.items()}
            _aux["data"] = (runtimes,
                             {name: _safe_version(name, (arg,)) for name, (arg, _) in CLI_SPECS.items()})
            _aux["at"] = time.monotonic()
        return _aux["data"]

def number_bytes(text):
    if not text: return None
    m = re.fullmatch(r"\s*(\d+)\s*", text)
    return int(m.group(1)) if m else None

def cgroup_memory_limit():
    for path in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        v = read_text(path, 128)
        if v and v != "max":
            n = number_bytes(v)
            if n and n < (1 << 60): return n
    return None

def cgroup_memory_used():
    for path in ("/sys/fs/cgroup/memory.current", "/sys/fs/cgroup/memory/memory.usage_in_bytes"):
        value = number_bytes(read_text(path, 128))
        if value is not None: return value
    return None

def cpu_quota():
    raw = read_text("/sys/fs/cgroup/cpu.max", 128)
    if raw:
        p = raw.split()
        try:
            if len(p) == 2 and p[0] != "max" and int(p[1]) > 0: return f"{int(p[0])/int(p[1]):.2f} 核（配额）"
        except ValueError: pass
    q, p = read_text("/sys/fs/cgroup/cpu/cpu.cfs_quota_us", 128), read_text("/sys/fs/cgroup/cpu/cpu.cfs_period_us", 128)
    try:
        if q and p and int(q) > 0 and int(p) > 0: return f"{int(q)/int(p):.2f} 核（配额）"
    except ValueError: pass
    return None

def _cpu_model():
    for line in (read_text("/proc/cpuinfo", 262144) or "").splitlines():
        if line.lower().startswith(("model name", "hardware")) and ":" in line: return line.split(":", 1)[1].strip()[:160]
    return platform.processor() or None

def get_port(environ=None):
    env = os.environ if environ is None else environ
    raw = env.get("PORT") if "PORT" in env else env.get("SERVER_PORT", "8000")
    if not isinstance(raw, str) or not re.fullmatch(r"[0-9]{1,5}", raw):
        raise ValueError("PORT/SERVER_PORT must be an integer from 1 to 65535")
    port = int(raw)
    if not 1 <= port <= 65535: raise ValueError("PORT/SERVER_PORT must be an integer from 1 to 65535")
    return port

def _network_details():
    if os.environ.get("STATUS_NETWORK_DETAILS") != "1": return None, None
    addresses=[]
    try:
        for ent in socket.getaddrinfo(socket.gethostname(), None, type=socket.SOCK_STREAM):
            ip=ent[4][0]
            if ip not in addresses and not ipaddress.ip_address(ip).is_loopback: addresses.append(ip)
    except (OSError, ValueError, IndexError): pass
    public_ip=None
    try:
        req=urllib.request.Request("https://api.ipify.org",headers={"User-Agent":"Cloady-status/1.0","Accept":"text/plain"})
        with urllib.request.urlopen(req,timeout=2) as resp: candidate=resp.read(64).decode("ascii","strict").strip()
        public_ip=str(ipaddress.ip_address(candidate))
    except Exception: pass
    return addresses or None, public_ip

def gather():
    now = dt.datetime.now().astimezone(); utc = dt.datetime.now(dt.timezone.utc); uname = platform.uname()
    distro = None
    for line in (read_text("/etc/os-release") or "").splitlines():
        if line.startswith("PRETTY_NAME="): distro = line.split("=", 1)[1].strip().strip('"')[:160]; break
    mem_total = mem_available = None
    for line in (read_text("/proc/meminfo") or "").splitlines():
        p = line.split()
        if len(p) >= 2 and p[0] in ("MemTotal:", "MemAvailable:"):
            try:
                if p[0] == "MemTotal:": mem_total = int(p[1])*1024
                else: mem_available = int(p[1])*1024
            except ValueError: pass
    load = None
    try: load = [round(float(v), 2) for v in os.getloadavg()]
    except (OSError, AttributeError): pass
    pressure = {k: read_text(f"/proc/pressure/{k}", 512) for k in ("cpu", "memory", "io")}
    pressure = {k:v for k,v in pressure.items() if v} or None
    disk = None
    try:
        d = shutil.disk_usage("/"); disk = {"path":"/", "total_bytes":d.total, "free_bytes":d.free, "used_bytes":d.used,
            "scope":"当前挂载命名空间可见的根文件系统；不一定代表宿主机全盘或独享额度"}
    except OSError: pass
    addresses, public_ip = _network_details()
    try: uptime_seconds = max(0, float((read_text("/proc/uptime", 128) or "").split()[0]))
    except (ValueError, IndexError): uptime_seconds = None
    runtimes, cli = _auxiliary()
    return {"generated_at_utc":utc.isoformat(), "time":{"utc":utc.isoformat(),"local":now.isoformat(),"timezone":str(now.tzinfo)},
        "os":{"system":uname.system or None,"distribution":distro,"release":uname.release or None,"kernel":uname.version or None,
              "machine":uname.machine or None,"scope":"当前进程可见；容器中可能反映宿主机内核"},
        "uptime":format_uptime(uptime_seconds), "uptime_seconds":uptime_seconds, "load_average_1_5_15":load,
        "pressure_stall_information":pressure,
        "cpu":{"model":_cpu_model(),"visible_logical_cores":os.cpu_count(),"container_quota":cpu_quota(),
               "scope":"型号/逻辑核数可能是宿主机可见值；容器 CPU 配额单独列示，无法识别时为 null"},
        "memory":{"host_visible_total_bytes":mem_total,"host_visible_available_bytes":mem_available,
           "container_used_bytes":cgroup_memory_used(),
           "container_limit_bytes":cgroup_memory_limit(),"scope":"Mem* 通常为宿主机可见值；容器限制来自 cgroup（若可读）"},
        "disk":disk,"runtimes":runtimes,"cli_tools":cli,
        "network":{"local_addresses":addresses or None,"public_egress_ip":public_ip,
          "public_ip_note":"仅在 STATUS_NETWORK_DETAILS=1 时查询；默认关闭时为 null（网络详情已停用），启用后无网络/查询失败也为 null"},
        "server_port":str(get_port())}

def format_uptime(seconds):
    if seconds is None: return None
    n = int(seconds); days, rem = divmod(n,86400); hours, rem = divmod(rem,3600); minutes, secs = divmod(rem,60)
    return f"{days} 天 {hours} 小时 {minutes} 分钟 {secs} 秒"

def format_bytes(value):
    if value is None or isinstance(value, bool): return "不可用"
    try: n = float(value)
    except (ValueError, TypeError, OverflowError): return "不可用"
    if not math.isfinite(n) or n < 0: return "不可用"
    if n == 0: return "0 B"
    units = ("B","KB","MB","GB","TB"); i=0
    while n >= 1024 and i < len(units)-1: n/=1024; i+=1
    if i and round(n, 2) >= 1024 and i < len(units)-1: n/=1024; i+=1
    return f"{n:.2f} {units[i]}" if i else f"{n:.0f} B"

def format_time(value):
    if not value: return "不可用"
    try:
        parsed=dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.strftime("%Y-%m-%d %H:%M:%S %Z") if parsed.tzinfo else parsed.strftime("%Y-%m-%d %H:%M:%S")+"（无时区）"
    except (ValueError, TypeError, AttributeError): return "不可用"

def percent(used, total):
    if used is None or total is None: return None
    try:
        used=float(used); total=float(total)
        if not math.isfinite(used) or not math.isfinite(total) or used < 0 or total <= 0: return None
        return round(min(100.0, max(0.0, used/total*100)), 1)
    except (ValueError, TypeError, OverflowError): return None

def status():
    with _lock:
        if _cache["data"] is None or time.monotonic()-_cache["at"] >= CACHE_SECONDS:
            _cache["data"]=gather(); _cache["at"]=time.monotonic()
        return _cache["data"]

def _v(x): return "不可用" if x is None or x == "" else str(x)
def _metric(label, value): return f'<div class="metric"><span>{html.escape(label)}</span><strong>{html.escape(_v(value))}</strong></div>'
def _byte_metric(label,value): return _metric(label,format_bytes(value))
def _section(title, body): return f'<section><h2>{html.escape(title)}</h2>{body}</section>'
def render_page(d):
    mem=d.get("memory") or {}; disk=d.get("disk") or {}; cpu=d.get("cpu") or {}; osd=d.get("os") or {}; net=d.get("network") or {}; tm=d.get("time") or {}
    mem_pct=percent(mem.get("container_used_bytes"),mem.get("container_limit_bytes"))
    membody="".join((_byte_metric("容器使用",mem.get("container_used_bytes")),_byte_metric("容器限制",mem.get("container_limit_bytes")),
        _metric("容器使用率",f"{mem_pct:.1f}%" if mem_pct is not None else None),
        _byte_metric("宿主机可见总内存",mem.get("host_visible_total_bytes")),_byte_metric("宿主机可见可用",mem.get("host_visible_available_bytes"))))
    diskbody="".join((_byte_metric("已用",disk.get("used_bytes")),_byte_metric("可用",disk.get("free_bytes")),_byte_metric("总量",disk.get("total_bytes")),
        _metric("使用率",(f'{percent(disk.get("used_bytes"),disk.get("total_bytes")):.1f}%' if percent(disk.get("used_bytes"),disk.get("total_bytes")) is not None else None)),_metric("挂载路径",disk.get("path"))))
    loads = d.get("load_average_1_5_15")
    load_text = " / ".join(f"{value:.2f}" for value in loads) if isinstance(loads, list) and all(isinstance(value, (int,float)) and math.isfinite(value) for value in loads) else None
    cpu_body="".join((_metric("处理器","未识别" if cpu.get("model") == "unknown" else cpu.get("model")),_metric("可见逻辑核",cpu.get("visible_logical_cores")),_metric("容器 CPU 配额",cpu.get("container_quota")),_metric("负载 1 / 5 / 15 分钟",load_text)))
    cli=[]
    for name, item in (d.get("cli_tools") or {}).items():
        state={"missing":"未安装","identified":"已安装","unknown":"已安装，版本未识别"}.get(item.get("status"),"状态不可用")
        suffix=(" · "+html.escape(_v(item.get("version")))) if item.get("version") else ""
        cli.append(f'<li><span>{html.escape(str(name))}</span><span>{state}{suffix}</span></li>')
    runtime=[]
    for name, value in (d.get("runtimes") or {}).items(): runtime.append(f'<li><span>{html.escape(str(name))}</span><span>{html.escape(_v(value))}</span></li>')
    details="".join((_metric("操作系统 / 发行版",f'{_v(osd.get("system"))} / {_v(osd.get("distribution"))}'),_metric("内核 / 架构",f'{_v(osd.get("kernel"))} / {_v(osd.get("machine"))}'),
      _metric("运行时间",d.get("uptime")),_metric("时区",tm.get("timezone")),_metric("本地时间",format_time(tm.get("local"))),_metric("UTC 时间",format_time(tm.get("utc")))))
    pressure=d.get("pressure_stall_information") or {}
    psibody="".join(_metric(k.upper(),format_psi(v)) for k,v in pressure.items()) or _metric("PSI","不可用")
    netbody="".join((_metric("本地地址",", ".join(map(str,net.get("local_addresses"))) if isinstance(net.get("local_addresses"),list) else net.get("local_addresses")),_metric("出口 IP",net.get("public_egress_ip")),_metric("监听端口",d.get("server_port"))))
    net_note="网络详情默认停用；只有设置 STATUS_NETWORK_DETAILS=1 才会尝试本地地址解析与固定 HTTPS 公网 IP 查询。null 可能表示已停用或不可用。"
    page='''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="color-scheme" content="dark"><title>Cloady 系统状态</title><style>
:root{color-scheme:dark;background:#090d16;color:#e7edf8;font:16px system-ui,-apple-system,"Segoe UI",sans-serif}*{box-sizing:border-box}body{max-width:1120px;margin:auto;padding:clamp(1rem,4vw,2.5rem)}header{padding:1rem 0 1.4rem}h1{font-size:clamp(1.8rem,5vw,2.7rem);margin:.2rem 0;color:#fff}h2{font-size:1.15rem;margin:0 0 1rem;color:#a5c8ff}.muted{color:#b4bfd1;line-height:1.6}main{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,310px),1fr));gap:1rem}section{background:#111a29;border:1px solid #2a3951;border-radius:14px;padding:1.1rem;box-shadow:0 8px 24px #0004}.metric{display:flex;justify-content:space-between;gap:1rem;padding:.65rem 0;border-bottom:1px solid #263348;align-items:flex-start}.metric:last-child{border:0}.metric span{color:#bac7da}.metric strong{text-align:right;overflow-wrap:anywhere;color:#f4f7fc;font-weight:600}ul{list-style:none;margin:0;padding:0}li{display:flex;justify-content:space-between;gap:.8rem;border-bottom:1px solid #263348;padding:.65rem 0}li:last-child{border:0}li span:last-child{text-align:right;overflow-wrap:anywhere;color:#d6e2f4}.wide{grid-column:1/-1}a{color:#9ac4ff}.foot{margin-top:1.3rem;font-size:.92rem}@media(max-width:430px){.metric,li{display:block}.metric strong,li span:last-child{display:block;text-align:left;margin-top:.25rem}}
.repo-link{overflow-wrap:anywhere}
</style></head><body><header><h1>系统状态</h1><p class="muted">只读状态概览 · 数据按当前进程与挂载命名空间可见范围采集。</p>
<p class="muted">公开源码仓库：<a class="repo-link" href="https://github.com/yincylan0/Cloady-status" target="_blank" rel="noopener noreferrer">https://github.com/yincylan0/Cloady-status</a></p></header><main>'''+_section("内存",membody)+_section("磁盘",diskbody)+_section("CPU 与负载",cpu_body)+_section("系统与时间",details)+_section("压力（PSI）",psibody)+_section("网络",netbody+'<p class="muted">'+html.escape(net_note)+'</p>')+_section("常用命令行工具",'<ul>'+''.join(cli)+'</ul>')+_section("语言运行时",'<ul>'+''.join(runtime)+'</ul>')+'''</main><p class="muted foot">内存与磁盘容量按 1024 进位显示（1 GB = 1024MB）。磁盘数据是当前挂载视图，不等同于独享磁盘额度。容器限制和宿主机可见值可能不同；逻辑核数仅表示可见核数，不代表独享核数；出口 IP 可能与入站 IP 不同。不可读项目标为“不可用”。</p><p><a href="/api/status">查看 JSON API</a></p><script src="/poll.js" defer></script></body></html>'''
    return page

def format_psi(raw):
    if not isinstance(raw, str): return "不可用"
    fields={}
    for line in raw.splitlines():
        parts=line.split()
        if not parts: continue
        kind=parts[0]
        if kind not in ("some", "full"): continue
        values=dict(part.split("=",1) for part in parts[1:] if "=" in part)
        avgs=[]
        for key, label in (("avg10", "10 秒均值"), ("avg60", "1 分钟均值"), ("avg300", "5 分钟均值")):
            try:
                value = float(values[key])
                if math.isfinite(value) and 0 <= value <= 100: avgs.append(f"{label} {value:.2f}%")
            except (KeyError, ValueError): pass
        if "total" in values:
            try:
                total = int(values['total'])
                if total >= 0: avgs.append(f"累计停顿 {total/1_000_000:.2f} 秒")
            except ValueError: pass
        if avgs: fields["部分任务" if kind == "some" else "全部任务"]="，".join(avgs)
    return "；".join(f"{k}: {v}" for k,v in fields.items()) or "不可用"

class Handler(BaseHTTPRequestHandler):
    server_version="StatusApp"; sys_version=""
    def log_message(self,fmt,*args): return
    def send_safe_headers(self,typ):
        self.send_header("Content-Type",typ); self.send_header("X-Content-Type-Options","nosniff"); self.send_header("X-Frame-Options","DENY")
        self.send_header("Referrer-Policy","no-referrer"); self.send_header("Content-Security-Policy","default-src 'none'; style-src 'unsafe-inline'; script-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'")
        self.send_header("Cache-Control","no-store"); self.send_header("Connection","close")
    def _respond(self,code,body,typ):
        self.send_response(code); self.send_safe_headers(typ); self.send_header("Content-Length",str(len(body))); self.end_headers()
        try: self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, TimeoutError): pass
    def do_GET(self):
        path=self.path.split("?",1)[0]
        if path=="/healthz": return self._respond(200,b"ok\n","text/plain; charset=utf-8")
        if path == "/poll.js":
            try:
                with open(os.path.join(os.path.dirname(__file__), "poll.js"), "rb") as script:
                    return self._respond(200, script.read(), "text/javascript; charset=utf-8")
            except OSError:
                return self._respond(404,b"not found\n","text/plain; charset=utf-8")
        if path not in ("/","/api/status"): return self._respond(404,b"not found\n","text/plain; charset=utf-8")
        try:
            data=status()
            if path=="/api/status": body=json.dumps(data,ensure_ascii=False,separators=(",",":" )).encode("utf-8"); typ="application/json; charset=utf-8"
            else: body=render_page(data).encode("utf-8"); typ="text/html; charset=utf-8"
            self._respond(200,body,typ)
        except Exception: self._respond(503,b'{"error":"status temporarily unavailable"}',"application/json; charset=utf-8")
    def do_POST(self): self._respond(405,b"method not allowed\n","text/plain; charset=utf-8")

class LimitedHTTPServer(ThreadingHTTPServer):
    daemon_threads=True; request_queue_size=16
    def __init__(self,address,handler): self._slots=threading.BoundedSemaphore(12); super().__init__(address,handler)
    def get_request(self):
        req,addr=super().get_request(); req.settimeout(8); return req,addr
    def process_request(self,request,client_address):
        if not self._slots.acquire(blocking=False): self.shutdown_request(request); return
        try: super().process_request(request,client_address)
        except Exception: self._slots.release(); raise
    def process_request_thread(self,request,client_address):
        try: super().process_request_thread(request,client_address)
        finally: self._slots.release()

def main():
    try: port=get_port()
    except ValueError as exc: raise SystemExit(str(exc))
    server=LimitedHTTPServer(("0.0.0.0",port),Handler)
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()
if __name__=="__main__": main()
