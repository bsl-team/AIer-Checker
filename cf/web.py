"""A small local web server for the teacher review desk.

Run ``cf-web`` (or ``python -m cf.web``), open the printed URL, and type the
contest folder path.  Everything stays on this machine: the server only reads
the folder the teacher chooses and the profile store; it never uploads code.
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import __version__
from .classroom import ClassroomError, SOURCE_EXTENSIONS, scan_contest
from .i18n import set_locale
from .store import ProfileStore, store_path
from .tokens import SPEC_BY_NAME

WEBUI_DIR = Path(__file__).resolve().with_name("webui")
MAX_BODY = 1_000_000


def _json_bytes(payload: object) -> bytes:
    return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")


class ReviewHandler(BaseHTTPRequestHandler):
    server_version = "AIerChecker/" + __version__

    # -- plumbing ------------------------------------------------------------
    def _send(self, status: int, payload: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    def _json(self, status: int, payload: object) -> None:
        self._send(status, _json_bytes(payload), "application/json; charset=utf-8")

    def _error(self, status: int, message: str) -> None:
        self._json(status, {"error": message})

    def _read_json(self) -> dict:
        raw_length = self.headers.get("Content-Length")
        try:
            length = int(raw_length or "0")
        except ValueError as exc:
            raise ClassroomError("请求长度无效") from exc
        if length > MAX_BODY:
            raise ClassroomError("请求体过大")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ClassroomError("请求不是有效的 JSON") from exc
        if not isinstance(data, dict):
            raise ClassroomError("请求必须是 JSON 对象")
        return data

    def log_message(self, fmt: str, *args) -> None:
        # Keep the terminal useful; the UI is the main feedback channel.
        print(f"[cf-web] {self.address_string()} {fmt % args}")

    # -- GET -----------------------------------------------------------------
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path == "/api/meta":
                self._api_meta()
            elif path == "/api/profiles":
                self._api_profiles(parse_qs(parsed.query))
            elif path == "/api/current":
                with self.server.lock:
                    scan = self.server.current_scan
                    payload = scan.to_dict() if scan is not None else None
                self._json(200, {"scan": payload})
            elif path == "/api/source":
                self._api_source(parse_qs(parsed.query))
            elif path == "/favicon.ico":
                self._send(204, b"", "image/x-icon")
            else:
                self._static(path)
        except ClassroomError as exc:
            self._error(400, str(exc))
        except Exception as exc:  # noqa: BLE001 - one bad request must not kill the server
            self._error(500, f"服务内部错误：{exc}")

    def _api_meta(self) -> None:
        default_store = ProfileStore(self.server.store_path).path
        self._json(200, {
            "version": __version__,
            "store": default_store,
            "languages": sorted(SPEC_BY_NAME),
            "extensions": sorted(SOURCE_EXTENSIONS),
            "locale": "zh",
            "max_body": MAX_BODY,
        })

    def _api_profiles(self, query: dict) -> None:
        explicit = (query.get("store") or [None])[0]
        store = ProfileStore(explicit or self.server.store_path)
        summaries = [s.__dict__ for s in store.summaries()]
        self._json(200, {"store": store.path, "profiles": summaries})

    def _api_source(self, query: dict) -> None:
        submission_id = (query.get("id") or [""])[0]
        if not submission_id:
            raise ClassroomError("缺少文件 id")
        with self.server.lock:
            scan = self.server.current_scan
            if scan is None:
                raise ClassroomError("请先扫描比赛文件夹")
            source = scan.source(submission_id)
        self._json(200, {"source": source})

    # -- POST ----------------------------------------------------------------
    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            if path == "/api/scan":
                self._api_scan()
            elif path == "/api/save-profiles":
                self._api_save()
            else:
                self._error(404, "接口不存在")
        except ClassroomError as exc:
            self._error(400, str(exc))
        except Exception as exc:  # noqa: BLE001
            self._error(500, f"服务内部错误：{exc}")

    def _api_scan(self) -> None:
        data = self._read_json()
        root = str(data.get("root") or "").strip()
        if not root:
            raise ClassroomError("请填写比赛文件夹路径")
        store = data.get("store") or self.server.store_path
        language = data.get("language") or "auto"
        sensitivity = data.get("sensitivity") or "standard"
        same_problem = bool(data.get("same_problem", True))
        include_same_user = bool(data.get("include_same_user", True))
        with self.server.lock:
            scan = scan_contest(
                root,
                store_path=store,
                language=language,
                sensitivity=sensitivity,
                same_problem=same_problem,
                include_same_user=include_same_user,
            )
            self.server.current_scan = scan
            self.server.store_path = scan.store_path
            payload = scan.to_dict()
        self._json(200, {"scan": payload})

    def _api_save(self) -> None:
        data = self._read_json()
        users = data.get("users")
        if users is not None and not isinstance(users, list):
            raise ClassroomError("users 必须是学生名单")
        mode = str(data.get("mode") or "merge")
        with self.server.lock:
            scan = self.server.current_scan
            if scan is None:
                raise ClassroomError("请先扫描比赛文件夹")
            saved = scan.save_profiles(users=users, mode=mode)
            payload = scan.to_dict()
        self._json(200, {"scan": payload, "saved": saved})

    # -- static --------------------------------------------------------------
    def _static(self, url_path: str) -> None:
        rel = url_path.lstrip("/") or "index.html"
        candidate = (WEBUI_DIR / rel).resolve()
        try:
            candidate.relative_to(WEBUI_DIR)
        except ValueError:
            self._error(403, "禁止访问该路径")
            return
        if not candidate.is_file():
            # The UI uses hash routes, so an unknown path can still be the app.
            candidate = WEBUI_DIR / "index.html"
        if not candidate.is_file():
            self._error(404, "前端文件缺失，请重新安装 cf")
            return
        mime, _ = mimetypes.guess_type(str(candidate))
        self._send(200, candidate.read_bytes(), mime or "application/octet-stream")


def serve(host: str = "127.0.0.1", port: int = 8765, *, open_browser: bool = False,
          store: str | None = None) -> int:
    set_locale(os.environ.get("CF_LANG") or "zh")
    try:
        httpd = ThreadingHTTPServer((host, port), ReviewHandler)
    except OSError as exc:
        print(f"无法启动本地服务：{exc}")
        print("端口可能被占用，请用 --port 0 让系统自动选择端口。")
        return 2
    httpd.daemon_threads = True
    httpd.lock = threading.RLock()
    httpd.current_scan = None
    httpd.store_path = store_path(store)
    actual_port = httpd.server_address[1]
    url = f"http://{host}:{actual_port}/"
    print(f"AIer Checker 教师审查台已启动：{url}")
    print(f"档案库：{httpd.store_path}")
    print("只在本机运行；按 Ctrl+C 停止。")
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        httpd.server_close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cf-web",
        description="AIer Checker 教师审查台（本地 Web 界面）",
    )
    parser.add_argument("--host", default="127.0.0.1", help="监听地址，默认只允许本机")
    parser.add_argument("--port", type=int, default=8765, help="端口；0 表示自动选择")
    parser.add_argument("--open", action="store_true", help="启动后打开浏览器")
    parser.add_argument("--store", default=None, help="档案库 JSON 路径")
    args = parser.parse_args(argv)
    return serve(args.host, args.port, open_browser=args.open, store=args.store)


if __name__ == "__main__":
    raise SystemExit(main())
