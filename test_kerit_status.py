import datetime as dt
import http.client
import io
import json
import math
import os
import pathlib
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock
from http.server import ThreadingHTTPServer

import kerit_status as app

class FormattingTests(unittest.TestCase):
    def test_bytes_boundaries_and_missing(self):
        cases={0:"0 B",1:"1 B",1000:"1000 B",1023:"1023 B",1024:"1.00 KB",1048575:"1.00 MB",1048576:"1.00 MB",1073741824:"1.00 GB",322239832064:"300.11 GB",1099511627775:"1.00 TB",1099511627776:"1.00 TB",None:"不可用",-1:"不可用",math.inf:"不可用",math.nan:"不可用","bad":"不可用"}
        for n,want in cases.items():
            with self.subTest(n=n): self.assertEqual(app.format_bytes(n),want)
    def test_uptime(self):
        self.assertEqual(app.format_uptime(None),None)
        self.assertEqual(app.format_uptime(0),"0 天 0 小时 0 分钟 0 秒")
        self.assertEqual(app.format_uptime(90061),"1 天 1 小时 1 分钟 1 秒")
    def test_time_timezone_no_microseconds(self):
        got=app.format_time("2025-01-02T03:04:05.123456+08:00")
        self.assertIn("2025-01-02 03:04:05",got); self.assertNotIn("123456",got); self.assertIn("UTC+08:00",got)
        self.assertEqual(app.format_time(None),"不可用")
        self.assertIn("无时区",app.format_time("2025-01-02T03:04:05"))
    def test_percent_safe(self):
        self.assertIsNone(app.percent(None,100)); self.assertIsNone(app.percent(0,0)); self.assertIsNone(app.percent(1,None))
        self.assertEqual(app.percent(0,100),0.0); self.assertEqual(app.percent(200,100),100.0); self.assertIsNone(app.percent(-1,100))
        self.assertIsNone(app.percent(float("nan"),100)); self.assertIsNone(app.percent(1,float("inf")))

class HtmlTests(unittest.TestCase):
    def sample(self):
        return {"os":{"system":"<script>alert(1)</script>","distribution":"ok","kernel":"k","machine":"m"},
          "memory":{"container_used_bytes":1073741824,"container_limit_bytes":2147483648,"host_visible_total_bytes":4294967296,"host_visible_available_bytes":1073741824},
          "disk":{"used_bytes":2,"free_bytes":8,"total_bytes":10,"path":"/"},"cpu":{"model":"cpu","visible_logical_cores":2},
          "time":{"timezone":"UTC","local":"2025-01-02T03:04:05+00:00","utc":"2025-01-02T03:04:05+00:00"},
          "uptime":"1 天","load_average_1_5_15":[0,0,0],"pressure_stall_information":None,
          "network":{"local_addresses":["192.0.2.1"]},"server_port":"8000",
          "cli_tools":{"curl":{"installed":True,"version":"8.0","status":"identified"},"wget":{"installed":False,"version":None,"status":"missing"}},"runtimes":{}}
    def test_escape_dark_cli_and_human_bytes_no_raw_json(self):
        page=app.render_page(self.sample())
        self.assertNotIn("<script>alert(1)</script>",page); self.assertIn("&lt;script&gt;",page)
        self.assertIn("color-scheme:dark",page); self.assertIn("常用命令行工具",page); self.assertIn("未安装",page)
        self.assertIn("1.00 GB",page); self.assertNotIn("1073741824",page)
        self.assertIn("1 GB = 1024MB",page)
        self.assertIn("容器使用率",page); self.assertIn("192.0.2.1",page)
        self.assertIn("CPU",page)

    def test_public_repository_link_is_visible_in_header(self):
        page=app.render_page(self.sample())
        header=page.split("</header>",1)[0]
        url="https://github.com/yincylan0/-kerit-status"
        self.assertIn("公开源码仓库：",header)
        self.assertIn(f'<a class="repo-link" href="{url}" target="_blank" rel="noopener noreferrer">{url}</a>',header)
        self.assertEqual(page.count(f'href="{url}"'),1)
        self.assertIn(".repo-link{overflow-wrap:anywhere}",page)
        self.assertIn("<title>Kerit 系统状态</title>",page)
        self.assertNotIn("ACLClouds",page)

class CliTests(unittest.TestCase):
    def test_missing_distinct_from_present_unknown(self):
        with mock.patch.object(app.shutil,"which",side_effect=[None,"/usr/bin/wget"]), mock.patch.object(app.subprocess,"Popen",side_effect=FileNotFoundError):
            missing=app._safe_version("curl",("--version",)); unknown=app._safe_version("wget",("--version",))
        self.assertEqual(missing,{"installed":False,"version":None,"status":"missing"})
        self.assertEqual(unknown,{"installed":True,"version":None,"status":"unknown"})
    def test_stderr_fixed_argv_no_shell_and_no_banner(self):
        with tempfile.TemporaryDirectory() as td:
            exe=pathlib.Path(td)/"ssh"; exe.write_text("#!/bin/sh\necho 'arbitrary secret banner'\necho 'OpenSSH_9.8p1, LibreSSL 3.3.6' >&2\n",encoding="utf8"); exe.chmod(0o755)
            with mock.patch.object(app.shutil,"which",return_value=str(exe)) as which:
                data=app._safe_version("ssh",("-V",))
            self.assertEqual(data["version"],"9.8p1"); self.assertNotIn("banner",str(data)); self.assertNotIn("secret",str(data))
            self.assertEqual(which.call_args.kwargs["path"],app._FIXED_PATH)
    def test_output_cap_and_timeout(self):
        with tempfile.TemporaryDirectory() as td:
            exe=pathlib.Path(td)/"curl"; exe.write_text("#!/bin/sh\npython3 -c 'import sys; sys.stdout.write(\"x\"*2000000); sys.stdout.flush()'\n",encoding="utf8"); exe.chmod(0o755)
            with mock.patch.object(app.shutil,"which",return_value=str(exe)):
                self.assertEqual(app._safe_version("curl",("--version",))["status"],"unknown")
            sleeper=pathlib.Path(td)/"slow"; sleeper.write_text("#!/bin/sh\nsleep 5\n",encoding="utf8"); sleeper.chmod(0o755)
            with mock.patch.object(app.shutil,"which",return_value=str(sleeper)):
                started=app.time.monotonic(); result=app._safe_version("curl",("--version",)); elapsed=app.time.monotonic()-started
            self.assertEqual(result["status"],"unknown"); self.assertLess(elapsed,3)

    def test_gather_runtimes_preserve_legacy_scalar_shape(self):
        def collected(name, _spec):
            return {"installed":True,"version":"22.1.0" if name=="node" else "3.12.1","status":"identified"}
        app._aux["data"]=None
        with mock.patch.object(app,"_safe_version",side_effect=collected), \
             mock.patch.object(app,"read_text",return_value=None), mock.patch.object(app.shutil,"disk_usage",side_effect=OSError), \
             mock.patch.object(app.socket,"getaddrinfo",side_effect=OSError), mock.patch.object(app.urllib.request,"urlopen",side_effect=OSError), \
             mock.patch.object(app.os,"getloadavg",side_effect=OSError):
            data=app.gather()
        self.assertEqual(data["runtimes"]["Python 3"],"Python 3.12.1")
        self.assertEqual(data["runtimes"]["Node.js"],"v22.1.0")
        self.assertIsInstance(data["runtimes"],dict)
        self.assertTrue(all(v is None or isinstance(v,str) for v in data["runtimes"].values()))
        self.assertIn("cli_tools",data)

class RegressionTests(unittest.TestCase):
    def test_runtime_binary_parser_and_minimal_environment(self):
        sentinel = "STATUS_SECRET_SENTINEL_NOT_FOR_OUTPUT"
        with tempfile.TemporaryDirectory() as td:
            python = pathlib.Path(td)/"python3"
            python.write_text('#!/bin/sh\nif [ -n "$STATUS_TEST_SECRET" ]; then echo "secret inherited"; else echo "Python 3.12.3"; fi\n')
            node = pathlib.Path(td)/"node"
            node.write_text('#!/bin/sh\necho "v18.19.1"\n')
            python.chmod(0o755); node.chmod(0o755)
            def lookup(binary, **_):
                return str(pathlib.Path(td)/binary) if binary in ("python3","node") else None
            app._aux["data"] = None
            try:
                with mock.patch.dict(os.environ,{"STATUS_TEST_SECRET":sentinel}), \
                     mock.patch.object(app.shutil,"which",side_effect=lookup), \
                     mock.patch.object(app.urllib.request,"urlopen",side_effect=OSError):
                    data = app.gather()
                self.assertEqual(data["runtimes"]["Python 3"],"Python 3.12.3")
                self.assertEqual(data["runtimes"]["Node.js"],"v18.19.1")
                self.assertIsNone(data["runtimes"]["Go"])
                self.assertNotIn(sentinel,json.dumps(data)+app.render_page(data))
                self.assertTrue(all(value is None or isinstance(value,str) for value in data["runtimes"].values()))
            finally:
                app._aux["data"] = None

    def test_zero_cgroup_memory_is_not_missing(self):
        with mock.patch.object(app,"read_text",side_effect=["0",None]) as read:
            self.assertEqual(app.cgroup_memory_used(),0)
            self.assertEqual(read.call_count,1)

    def test_readable_psi_and_load_list(self):
        value = app.format_psi("some avg10=0.50 avg60=0.20 avg300=0.10 total=1500000")
        self.assertIn("10 秒均值 0.50%",value)
        self.assertIn("累计停顿 1.50 秒",value)
        self.assertNotIn("avgavg",value)
        self.assertEqual(app.format_psi("unrecognized secret=hello"),"不可用")
        page=app.render_page(HtmlTests().sample())
        self.assertIn("0.00 / 0.00 / 0.00",page)
        self.assertNotIn("[0, 0, 0]",page)
        self.assertNotIn("未安装 · 不可用",page)

class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server=ThreadingHTTPServer(("127.0.0.1",0),app.Handler)
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True); cls.thread.start()
    @classmethod
    def tearDownClass(cls): cls.server.shutdown(); cls.server.server_close(); cls.thread.join()
    def request(self,method,path):
        c=http.client.HTTPConnection("127.0.0.1",self.server.server_port,timeout=3); c.request(method,path); r=c.getresponse(); body=r.read(); headers=dict(r.getheaders()); c.close(); return r.status,headers,body
    @mock.patch.object(app,"status",return_value={"uptime_seconds":42,"disk":{"used_bytes":1024},"memory":{"host_visible_total_bytes":4096}})
    def test_api_preserves_raw_numbers(self,_):
        code,h,b=self.request("GET","/api/status"); data=json.loads(b)
        self.assertEqual(code,200); self.assertEqual(data["uptime_seconds"],42); self.assertEqual(data["disk"]["used_bytes"],1024)
        self.assertEqual(data["memory"]["host_visible_total_bytes"],4096); self.assertEqual(h["X-Content-Type-Options"],"nosniff")
    def test_health_404_post405(self):
        code,_,body=self.request("GET","/healthz"); self.assertEqual((code,body),(200,b"ok\n"))
        self.assertEqual(self.request("GET","/bad")[0],404); self.assertEqual(self.request("POST","/")[0],405)
    @mock.patch.object(app,"status",return_value=HtmlTests().sample())
    def test_root_html(self,_):
        code,h,b=self.request("GET","/"); self.assertEqual(code,200); self.assertIn("text/html",h["Content-Type"]); self.assertIn(b"color-scheme:dark",b)
        self.assertIn(b'<script src="/poll.js" defer></script>',b)
        self.assertIn("script-src 'self'",h["Content-Security-Policy"]); self.assertIn("connect-src 'self'",h["Content-Security-Policy"])
    def test_poll_script_is_same_origin_and_served_no_store(self):
        code,h,b=self.request("GET","/poll.js")
        self.assertEqual(code,200); self.assertIn("text/javascript",h["Content-Type"]); self.assertEqual(h["Cache-Control"],"no-store")
        self.assertIn(b"setInterval(refresh, INTERVAL_MS)",b); self.assertEqual(self.request("GET","/poll.js/extra")[0],404)

class PortAndNetworkTests(unittest.TestCase):
    def test_port_precedence_fallback_and_validation(self):
        self.assertEqual(app.get_port({"PORT":"8123","SERVER_PORT":"9000"}),8123); self.assertEqual(app.get_port({"SERVER_PORT":"9000"}),9000); self.assertEqual(app.get_port({}),8000)
        self.assertEqual(app.get_port({"PORT":"1"}),1); self.assertEqual(app.get_port({"PORT":"65535"}),65535)
        for val in ("0","65536","+80"," 80","abc","1.2","100000"):
            with self.subTest(val=val), self.assertRaises(ValueError): app.get_port({"PORT":val})
        with self.assertRaises(ValueError): app.get_port({"PORT":"","SERVER_PORT":"8000"})
    def test_gather_port_is_string_effective_value(self):
        with mock.patch.dict(os.environ,{"PORT":"8123","SERVER_PORT":"9000","STATUS_NETWORK_DETAILS":"0"}), mock.patch.object(app,"_auxiliary",return_value=({},{})), mock.patch.object(app,"read_text",return_value=None), mock.patch.object(app.shutil,"disk_usage",side_effect=OSError): data=app.gather()
        self.assertEqual(data["server_port"],"8123")
    def test_network_disabled_without_any_network_call(self):
        with mock.patch.dict(os.environ,{},clear=True), mock.patch.object(app.socket,"getaddrinfo",side_effect=AssertionError), mock.patch.object(app.urllib.request,"urlopen",side_effect=AssertionError): self.assertEqual(app._network_details(),(None,None))
    def test_network_opt_in_and_failure(self):
        with mock.patch.dict(os.environ,{"STATUS_NETWORK_DETAILS":"1"}), mock.patch.object(app.socket,"getaddrinfo",return_value=[(None,None,None,None,("192.0.2.4",0))]), mock.patch.object(app.urllib.request,"urlopen",side_effect=OSError): self.assertEqual(app._network_details(),(["192.0.2.4"],None))
        for value in ("true","yes","0",""):
            with mock.patch.dict(os.environ,{"STATUS_NETWORK_DETAILS":value}), mock.patch.object(app.socket,"getaddrinfo",side_effect=AssertionError): self.assertEqual(app._network_details(),(None,None))
    def test_public_ip_must_parse_as_ip(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self,*args): pass
            def read(self,_): return b"not-an-ip"
        with mock.patch.dict(os.environ,{"STATUS_NETWORK_DETAILS":"1"}), mock.patch.object(app.socket,"getaddrinfo",return_value=[]), mock.patch.object(app.urllib.request,"urlopen",return_value=Response()): self.assertEqual(app._network_details(),(None,None))

class IsolatedProcessSmokeTests(unittest.TestCase):
    def test_real_server_port_and_health(self):
        import time
        probe=ThreadingHTTPServer(("127.0.0.1",0),app.Handler)
        port=probe.server_port; probe.server_close()
        env={"PATH":os.defpath,"PORT":str(port),"SERVER_PORT":str(port+1),"STATUS_NETWORK_DETAILS":"0","PYTHONIOENCODING":"utf-8"}
        proc=subprocess.Popen([sys.executable,"-I","-B","-u",str(pathlib.Path(__file__).with_name("kerit_status.py"))],cwd="/",env=env,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
        try:
            result=None
            for _ in range(60):
                if proc.poll() is not None: self.fail("server exited before smoke request")
                try:
                    c=http.client.HTTPConnection("127.0.0.1",port,timeout=1); c.request("GET","/healthz"); r=c.getresponse(); body=r.read(); c.close()
                    if r.status==200: result=(r.status,body); break
                except OSError: time.sleep(.1)
            self.assertEqual(result,(200,b"ok\n"))
            c=http.client.HTTPConnection("127.0.0.1",port,timeout=5); c.request("GET","/api/status"); r=c.getresponse(); data=json.loads(r.read()); c.close()
            self.assertEqual(data["server_port"],str(port))
            self.assertIsNone(data["network"]["local_addresses"]); self.assertIsNone(data["network"]["public_egress_ip"])
        finally:
            proc.terminate()
            try: proc.wait(timeout=3)
            except subprocess.TimeoutExpired: proc.kill(); proc.wait(timeout=2)
            if proc.stderr: proc.stderr.close()

if __name__=="__main__": unittest.main()
