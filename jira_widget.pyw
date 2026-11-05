"""Jira 기한 임박 위젯 — 바탕화면에 항상 떠 있는 포스트잇.

Windows Python(표준 라이브러리만) + tkinter. pythonw 로 실행하면 콘솔이 뜨지 않는다.

    python  jira_widget.pyw --install     # 설치: %LOCALAPPDATA%\\JiraWidget 로 복사, 시작프로그램·바탕화면 바로가기, 실행
    python  jira_widget.pyw --uninstall   # 제거: 바로가기·설치본·설정·저장된 토큰 삭제
    pythonw jira_widget.pyw               # 실행
    python  jira_widget.pyw --demo        # 가짜 데이터로 화면만 확인

인증: Jira API 토큰(https://id.atlassian.com/manage-profile/security/api-tokens).
첫 실행 때 입력 창이 뜨고, Windows 자격 증명 관리자에 저장한다. 파일에는 남기지 않는다.
설정(위치·JQL 등): %APPDATA%\\JiraWidget\\config.json

조작: 머리줄 드래그 = 이동 / 좌우 가장자리 드래그 = 폭 조절 / 항목 클릭 = 브라우저로 이슈 열기 / 우클릭 = 메뉴
"""

import base64
import re
import ctypes
import datetime as dt
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import tkinter as tk
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from ctypes import wintypes

APP = "JiraWidget"
LNK_NAME = "Jira 기한 위젯.lnk"
CRED_TARGET = "JiraWidget:atlassian"
CONF_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), APP)
CONF_PATH = os.path.join(CONF_DIR, "config.json")

DEFAULTS = {
    "site": "",     # 예: https://your-domain.atlassian.net — 첫 실행 창에서 입력
    "jql": "assignee = currentUser() AND statusCategory != Done AND duedate is not EMPTY ORDER BY duedate ASC, created ASC",
    "jql_no_due": "assignee = currentUser() AND statusCategory = \"In Progress\" AND duedate is EMPTY",
    "max_items": 7,
    "refresh_min": 10,
    "x": None,
    "y": None,
    "topmost": True,
    "alpha": 0.94,
    "width": 340,   # 화면 배율 적용 전 폭. 좌우 가장자리를 끌어 바꾼다
}

# 색 — 어두운 바탕 위에서 대비가 나도록 고른 값
BG = "#1e2227"
BG_ROW_HOVER = "#2a2f36"
FG = "#e6e8eb"
FG_DIM = "#8b929a"
TRACK = "#353b43"
C_OVER = "#e5534b"   # 기한 지남
C_HOT = "#f0883e"    # 2일 이내
C_WARM = "#d4a72c"   # 7일 이내
C_OK = "#57ab5a"     # 여유
FONT = "Malgun Gothic"
MIN_W, MAX_W = 260, 1000


# ---------------------------------------------------------------- 설정

def load_conf():
    conf = dict(DEFAULTS)
    try:
        with open(CONF_PATH, encoding="utf-8") as f:
            conf.update(json.load(f))
    except (OSError, ValueError):
        pass
    return conf


DEMO = False


def save_conf(conf):
    if DEMO:  # 데모는 실제 설정을 건드리지 않는다
        return
    try:
        os.makedirs(CONF_DIR, exist_ok=True)
        with open(CONF_PATH, "w", encoding="utf-8") as f:
            json.dump(conf, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


# ---------------------------------------------------------------- 자격 증명 관리자

class FILETIME(ctypes.Structure):
    _fields_ = [("lo", wintypes.DWORD), ("hi", wintypes.DWORD)]


class CREDENTIAL(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


CRED_TYPE_GENERIC = 1
CRED_PERSIST_LOCAL_MACHINE = 2


def cred_read():
    """(email, token) 또는 None."""
    adv = ctypes.windll.advapi32
    p = ctypes.POINTER(CREDENTIAL)()
    if not adv.CredReadW(CRED_TARGET, CRED_TYPE_GENERIC, 0, ctypes.byref(p)):
        return None
    try:
        c = p.contents
        token = ctypes.string_at(c.CredentialBlob, c.CredentialBlobSize).decode("utf-8")
        return c.UserName, token
    finally:
        adv.CredFree(p)


def cred_write(email, token):
    blob = token.encode("utf-8")
    buf = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
    c = CREDENTIAL()
    c.Type = CRED_TYPE_GENERIC
    c.TargetName = CRED_TARGET
    c.CredentialBlobSize = len(blob)
    c.CredentialBlob = ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte))
    c.Persist = CRED_PERSIST_LOCAL_MACHINE
    c.UserName = email
    if not ctypes.windll.advapi32.CredWriteW(ctypes.byref(c), 0):
        raise OSError(ctypes.GetLastError(), "CredWriteW 실패")


def cred_delete():
    ctypes.windll.advapi32.CredDeleteW(CRED_TARGET, CRED_TYPE_GENERIC, 0)


# ---------------------------------------------------------------- Jira

class JiraError(Exception):
    pass


SITE_RE = re.compile(r"^https://[A-Za-z0-9.-]+(:\d+)?$")


def normalize_site(site):
    """'your-domain.atlassian.net' → 'https://your-domain.atlassian.net'. https 가 아니면 None.

    토큰을 Basic 인증 헤더로 보내므로 평문 http 는 받지 않는다.
    """
    site = (site or "").strip().rstrip("/")
    if site and "://" not in site:
        site = "https://" + site
    return site if SITE_RE.match(site) else None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # urllib 은 리다이렉트할 때 Authorization 헤더를 다른 호스트로도 그대로 넘긴다.
    # 토큰이 엉뚱한 곳으로 새지 않도록 리다이렉트를 따라가지 않는다.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def jira_call(site, email, token, path, params=None, body=None):
    site = normalize_site(site)
    if not site:
        raise JiraError("Jira 주소가 올바르지 않음 — 우클릭 › 로그인 정보 다시 입력")
    url = site + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    auth = base64.b64encode(f"{email}:{token}".encode()).decode()
    headers = {"Authorization": "Basic " + auth, "Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with _opener.open(req, timeout=20) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise JiraError("인증 실패 — 우클릭 › 로그인 정보 다시 입력") from e
        if 300 <= e.code < 400:
            raise JiraError("Jira 주소가 다른 곳으로 넘어감 — 사이트 주소 확인") from e
        raise JiraError(f"Jira 오류 HTTP {e.code}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise JiraError("연결 실패 — 네트워크 확인") from e


def fetch(conf, email, token):
    res = jira_call(conf["site"], email, token, "/rest/api/3/search/jql", {
        "jql": conf["jql"],
        "maxResults": conf["max_items"],
        "fields": "summary,duedate,created,issuetype",
    })
    items = []
    for it in res.get("issues", []):
        f = it["fields"]
        items.append({
            "key": it["key"],
            "summary": f.get("summary") or "",
            "due": f.get("duedate"),
            "created": (f.get("created") or "")[:10],
            "epic": (f.get("issuetype") or {}).get("hierarchyLevel") == 1,
        })
    no_due = None
    try:
        r = jira_call(conf["site"], email, token, "/rest/api/3/search/approximate-count",
                      body={"jql": conf["jql_no_due"]})
        no_due = r.get("count")
    except JiraError:
        pass
    return items, no_due


def demo_data():
    t = dt.date.today()
    d = lambda n: (t + dt.timedelta(days=n)).isoformat()
    return [
        {"key": "DEMO-12", "summary": "분기 보고서 초안 검토 의견 반영", "due": d(-2), "created": d(-9), "epic": False},
        {"key": "DEMO-31", "summary": "배포 파이프라인 승인 단계 추가", "due": d(0), "created": d(-15), "epic": False},
        {"key": "DEMO-45", "summary": "고객 문의 회신 — 요금 산정 기준 설명", "due": d(2), "created": d(-1), "epic": False},
        {"key": "DEMO-52", "summary": "모니터링 알림 임계값 조정과 담당자 지정", "due": d(6), "created": d(-3), "epic": False},
        {"key": "DEMO-7", "summary": "레거시 인증 방식 전환", "due": d(25), "created": d(-70), "epic": True},
        {"key": "DEMO-60", "summary": "운영 매뉴얼 개정판 작성과 팀 공유", "due": d(39), "created": d(-40), "epic": False},
        {"key": "DEMO-66", "summary": "연간 계약 갱신 일정 확인", "due": d(149), "created": d(-5), "epic": False},
    ], 83


# ---------------------------------------------------------------- 계산

def status_of(item, today):
    due = dt.date.fromisoformat(item["due"])
    left = (due - today).days
    try:
        start = dt.date.fromisoformat(item["created"])
    except ValueError:
        start = today
    span = (due - start).days + 1          # 기한 당일까지 포함
    used = (today - start).days + 1
    frac = 1.0 if span <= 0 else max(0.0, min(1.0, used / span))
    if left < 0:
        color, label, frac = C_OVER, f"D+{-left}", 1.0
    elif left == 0:
        color, label = C_OVER, "D-day"
    elif left <= 2:
        color, label = C_HOT, f"D-{left}"
    elif left <= 7:
        color, label = C_WARM, f"D-{left}"
    else:
        color, label = C_OK, f"D-{left}"
    return frac, color, label, due


# ---------------------------------------------------------------- 화면

class Widget:
    def __init__(self, root, conf, demo=False):
        self.root, self.conf, self.demo = root, conf, demo
        self.q = queue.Queue()
        self.cred = None
        self.items, self.no_due, self.error, self.updated = [], None, None, None
        self.loading = False
        self.s = root.winfo_fpixels("1i") / 96.0   # 화면 배율(125% → 1.25)
        self.W = self.px(max(MIN_W, min(MAX_W, int(conf.get("width") or 340))))
        self.P = self.px(12)

        root.overrideredirect(True)
        root.configure(bg=BG)
        root.attributes("-topmost", bool(conf["topmost"]))
        root.attributes("-alpha", conf["alpha"])
        x = conf["x"] if conf["x"] is not None else root.winfo_screenwidth() - self.W - self.px(24)
        y = conf["y"] if conf["y"] is not None else self.px(80)
        root.geometry(f"+{x}+{y}")

        # 머리줄
        self.head = tk.Frame(root, bg=BG)
        self.head.pack(fill="x", padx=self.P, pady=(self.px(10), self.px(4)))
        self.title = tk.Label(self.head, text="기한 임박", bg=BG, fg=FG,
                              font=(FONT, 10, "bold"))
        self.title.pack(side="left")
        self.stamp = tk.Label(self.head, text="", bg=BG, fg=FG_DIM, font=(FONT, 8))
        self.stamp.pack(side="left", padx=(8, 0))
        btn_close = tk.Label(self.head, text="✕", bg=BG, fg=FG_DIM, font=(FONT, 9), cursor="hand2")
        btn_close.pack(side="right")
        btn_close.bind("<Button-1>", lambda e: self.quit())
        btn_ref = tk.Label(self.head, text="⟳", bg=BG, fg=FG_DIM, font=(FONT, 10), cursor="hand2")
        btn_ref.pack(side="right", padx=(0, 8))
        btn_ref.bind("<Button-1>", lambda e: self.refresh())
        for w in (self.head, self.title, self.stamp):
            w.bind("<ButtonPress-1>", self._drag_start)
            w.bind("<B1-Motion>", self._drag_move)
            w.bind("<ButtonRelease-1>", self._drag_end)

        self.body = tk.Frame(root, bg=BG)
        self.body.pack(fill="both", padx=self.P)
        self.foot = tk.Label(root, text="", bg=BG, fg=FG_DIM, font=(FONT, 8), anchor="w")
        self.foot.pack(fill="x", padx=self.P, pady=(self.px(4), self.px(10)))

        self.menu = tk.Menu(root, tearoff=0)
        self.menu.add_command(label="새로고침", command=self.refresh)
        self.topvar = tk.BooleanVar(value=bool(conf["topmost"]))
        self.menu.add_checkbutton(label="항상 위", variable=self.topvar, command=self._toggle_top)
        self.menu.add_command(label="Jira 에서 전체 보기", command=self._open_list)
        self.menu.add_separator()
        self.menu.add_command(label="로그인 정보 다시 입력", command=self._reset_token)
        self.menu.add_command(label="설정 파일 열기", command=self._open_conf)
        self.menu.add_separator()
        self.menu.add_command(label="종료", command=self.quit)
        root.bind_all("<Button-3>", lambda e: self.menu.tk_popup(e.x_root, e.y_root))

        # 좌우 가장자리 손잡이 — 테두리 없는 창이라 OS 크기 조절이 없어 직접 만든다
        for side in ("left", "right"):
            g = tk.Frame(root, bg=BG, width=self.px(6), cursor="sb_h_double_arrow")
            if side == "left":
                g.place(x=0, y=0, relheight=1)
            else:
                g.place(relx=1, x=-self.px(6), y=0, relheight=1)
            g.bind("<ButtonPress-1>", lambda e, sd=side: self._resize_start(e, sd))
            g.bind("<B1-Motion>", self._resize_move)
            g.bind("<ButtonRelease-1>", self._resize_end)

        self.render()
        root.after(100, self._poll)
        if demo:
            self.items, self.no_due = demo_data()
            self.updated = dt.datetime.now()
            self.render()
        else:
            root.after(200, self._ensure_cred_then_refresh)

    def px(self, n):
        return int(round(n * self.s))

    # --- 이동
    def _drag_start(self, e):
        self._dx, self._dy = e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y()

    def _drag_move(self, e):
        self.root.geometry(f"+{e.x_root - self._dx}+{e.y_root - self._dy}")

    def _drag_end(self, e):
        self.conf["x"], self.conf["y"] = self.root.winfo_x(), self.root.winfo_y()
        save_conf(self.conf)

    # --- 폭 조절
    def _resize_start(self, e, side):
        self._rs = (side, e.x_root, self.W, self.root.winfo_x())

    def _resize_move(self, e):
        side, x0, w0, wx0 = self._rs
        dx = e.x_root - x0
        w = w0 + dx if side == "right" else w0 - dx
        w = max(self.px(MIN_W), min(self.px(MAX_W), w))
        x = wx0 if side == "right" else wx0 + (w0 - w)
        self.W = w
        self.root.geometry(f"{w}x{self.root.winfo_height()}+{x}+{self.root.winfo_y()}")

    def _resize_end(self, e):
        self.conf["width"] = int(round(self.W / self.s))
        self.conf["x"], self.conf["y"] = self.root.winfo_x(), self.root.winfo_y()
        save_conf(self.conf)
        self.render()   # 줄임표와 막대 길이를 새 폭에 맞춰 다시 그린다

    # --- 메뉴 동작
    def _toggle_top(self):
        self.conf["topmost"] = self.topvar.get()
        self.root.attributes("-topmost", self.conf["topmost"])
        save_conf(self.conf)

    def _open_list(self):
        site = normalize_site(self.conf["site"])
        if site:
            webbrowser.open(site + "/issues/?jql=" + urllib.parse.quote(self.conf["jql"]))

    def _open_conf(self):
        save_conf(self.conf)
        os.startfile(CONF_PATH)

    def _reset_token(self):
        cred_delete()
        self.cred = None
        self._ask_cred()

    def quit(self):
        save_conf(self.conf)
        self.root.destroy()

    # --- 인증
    def _ensure_cred_then_refresh(self):
        self.cred = cred_read()
        if self.cred and normalize_site(self.conf["site"]):
            self.refresh()
        else:
            self._ask_cred()

    def _ask_cred(self):
        d = tk.Toplevel(self.root)
        d.title("Jira 위젯 — 로그인")
        d.attributes("-topmost", True)
        d.configure(bg=BG, padx=16, pady=14)
        d.resizable(False, False)
        lab = lambda t, **k: tk.Label(d, text=t, bg=BG, fg=k.pop("fg", FG), font=(FONT, 9), anchor="w", justify="left", **k)
        lab("Jira 사이트 주소").pack(fill="x")
        e_site = tk.Entry(d, width=40, font=(FONT, 10))
        e_site.pack(fill="x", pady=(2, 8))
        e_site.insert(0, self.conf.get("site") or "https://your-domain.atlassian.net")
        lab("Atlassian 이메일").pack(fill="x")
        e_mail = tk.Entry(d, width=40, font=(FONT, 10))
        e_mail.pack(fill="x", pady=(2, 8))
        prev = cred_read()
        if prev:
            e_mail.insert(0, prev[0])
        lab("API 토큰").pack(fill="x")
        e_tok = tk.Entry(d, width=40, show="•", font=(FONT, 10))
        e_tok.pack(fill="x", pady=(2, 4))
        link = lab("토큰 만들기 (id.atlassian.com) ↗", fg="#58a6ff", cursor="hand2")
        link.pack(fill="x")
        link.bind("<Button-1>", lambda e: webbrowser.open(
            "https://id.atlassian.com/manage-profile/security/api-tokens"))
        lab("토큰은 Windows 자격 증명 관리자에만 저장됩니다.", fg=FG_DIM).pack(fill="x", pady=(6, 0))
        msg = lab("", fg=C_OVER)
        msg.pack(fill="x", pady=(4, 0))

        def ok(_=None):
            email, tok = e_mail.get().strip(), e_tok.get().strip()
            site = normalize_site(e_site.get())
            if not site or "your-domain" in site:
                msg.config(text="사이트 주소를 https://<도메인> 형태로 넣어 주세요.", fg=C_OVER)
                return
            if not email or not tok:
                msg.config(text="이메일과 토큰을 모두 넣어 주세요.", fg=C_OVER)
                return
            msg.config(text="확인 중…", fg=FG_DIM)
            d.update()
            try:
                jira_call(site, email, tok, "/rest/api/3/myself")
            except JiraError as ex:
                msg.config(text=str(ex).split(" — ")[0], fg=C_OVER)
                return
            cred_write(email, tok)
            self.cred = (email, tok)
            self.conf["site"] = site
            save_conf(self.conf)
            d.destroy()
            self.refresh()

        tk.Button(d, text="저장", command=ok, width=10).pack(anchor="e", pady=(8, 0))
        d.bind("<Return>", ok)
        (e_tok if prev else e_site).focus_set()

    # --- 갱신
    def refresh(self):
        if self.demo:
            return
        if not self.cred:
            self._ensure_cred_then_refresh()
            return
        if self.loading:
            return
        self.loading = True
        self.stamp.config(text="갱신 중…")
        conf, (email, tok) = dict(self.conf), self.cred

        def work():
            try:
                self.q.put(("ok", fetch(conf, email, tok)))
            except JiraError as e:
                self.q.put(("err", str(e)))
            except Exception as e:  # 위젯은 죽지 않는다
                self.q.put(("err", f"오류: {type(e).__name__}"))

        threading.Thread(target=work, daemon=True).start()

    def _poll(self):
        try:
            kind, val = self.q.get_nowait()
        except queue.Empty:
            pass
        else:
            self.loading = False
            if kind == "ok":
                self.items, self.no_due = val
                self.error = None
                self.updated = dt.datetime.now()
            else:
                self.error = val
            self.render()
            self.root.after(self.conf["refresh_min"] * 60_000, self.refresh)
        self.root.after(200, self._poll)

    # --- 그리기
    def render(self):
        for w in self.body.winfo_children():
            w.destroy()
        today = dt.date.today()
        if self.updated:
            self.stamp.config(text=self.updated.strftime("%H:%M 갱신"))
        if self.error:
            tk.Label(self.body, text=self.error, bg=BG, fg=C_OVER, font=(FONT, 9),
                     anchor="w", justify="left", wraplength=self.W - 2 * self.P).pack(fill="x", pady=4)
        if not self.items and not self.error:
            tk.Label(self.body, text="불러오는 중…" if not self.updated else "기한이 있는 일이 없습니다.",
                     bg=BG, fg=FG_DIM, font=(FONT, 9), anchor="w").pack(fill="x", pady=4)
        for it in self.items:
            self._row(it, today)
        n_over = sum(1 for it in self.items if dt.date.fromisoformat(it["due"]) < today)
        self.title.config(text=f"기한 임박 · 지남 {n_over}" if n_over else "기한 임박",
                          fg=C_OVER if n_over else FG)
        self.foot.config(text=f"기한 없이 진행 중 {self.no_due}건" if self.no_due is not None else "")
        # 폭은 고정, 높이는 내용에 맞춘다
        self.root.update_idletasks()
        h = self.root.winfo_reqheight()
        self.root.geometry(f"{self.W}x{h}+{self.root.winfo_x()}+{self.root.winfo_y()}")

    def _row(self, it, today):
        frac, color, label, due = status_of(it, today)
        url = f"{normalize_site(self.conf['site'])}/browse/{urllib.parse.quote(it['key'])}"
        row = tk.Frame(self.body, bg=BG, cursor="hand2")
        row.pack(fill="x", pady=self.px(3))
        top = tk.Frame(row, bg=BG)
        top.pack(fill="x")
        key = tk.Label(top, text=it["key"].split("-")[-1], bg=BG, fg=FG_DIM, font=(FONT, 8), width=4, anchor="w")
        key.pack(side="left")
        dl = tk.Label(top, text=label, bg=BG, fg=color, font=(FONT, 9, "bold"), anchor="e")
        dl.pack(side="right")
        due_l = tk.Label(top, text=due.strftime("%m/%d"), bg=BG, fg=FG_DIM, font=(FONT, 8))
        due_l.pack(side="right", padx=(0, self.px(6)))
        summ = ("◆ " if it["epic"] else "") + it["summary"]
        sl = tk.Label(top, text=summ, bg=BG, fg=FG, font=(FONT, 9), anchor="w", width=1)
        sl.pack(side="left", fill="x", expand=True)
        top.update_idletasks()
        used = sum(w.winfo_reqwidth() for w in (key, dl, due_l)) + self.px(6 + 8)
        self._elide(sl, summ, self.W - 2 * self.P - used)

        bh, bw = self.px(5), self.W - 2 * self.P
        bar = tk.Canvas(row, width=bw, height=bh, bg=BG, highlightthickness=0, bd=0)
        bar.pack(fill="x", pady=(self.px(2), 0))
        bar.create_rectangle(0, 0, bw, bh, fill=TRACK, width=0)
        bar.create_rectangle(0, 0, max(self.px(2), int(bw * frac)), bh, fill=color, width=0)

        widgets = (row, top, key, dl, due_l, sl, bar)
        for w in widgets:
            w.bind("<Button-1>", lambda e, u=url: webbrowser.open(u))
            w.bind("<Enter>", lambda e, ws=widgets: [x.config(bg=BG_ROW_HOVER) for x in ws if not isinstance(x, tk.Canvas)])
            w.bind("<Leave>", lambda e, ws=widgets: [x.config(bg=BG) for x in ws if not isinstance(x, tk.Canvas)])

    def _elide(self, label, text, max_px):
        import tkinter.font as tkf
        f = tkf.Font(font=label.cget("font"))
        if f.measure(text) <= max_px:
            return
        while text and f.measure(text + "…") > max_px:
            text = text[:-1]
        label.config(text=text + "…")


# ---------------------------------------------------------------- 설치

def install():
    """%LOCALAPPDATA%\\JiraWidget 로 복사하고 시작프로그램·바탕화면 바로가기를 만든 뒤 실행한다."""
    dest_dir = os.path.join(os.environ["LOCALAPPDATA"], APP)
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, "jira_widget.pyw")
    shutil.copy2(os.path.abspath(__file__), dest)
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.exists(pyw):
        pyw = sys.executable
    q = lambda v: v.replace("'", "''")   # PowerShell 작은따옴표 문자열 이스케이프
    # 바탕화면은 OneDrive 등으로 옮겨져 있을 수 있어 셸에게 경로를 묻는다
    ps = []
    for folder in ("Startup", "Desktop"):
        ps.append(
            f"$s=$w.CreateShortcut([IO.Path]::Combine([Environment]::GetFolderPath('{folder}'),"
            f"'{LNK_NAME}'));$s.TargetPath='{q(pyw)}';"
            f"$s.Arguments='\"{q(dest)}\"';$s.WorkingDirectory='{q(dest_dir)}';$s.Save();"
        )
    subprocess.run(["powershell", "-NoProfile", "-Command",
                    "$w=New-Object -ComObject WScript.Shell;" + "".join(ps)], check=True)
    subprocess.Popen([pyw, dest], cwd=dest_dir, creationflags=0x00000008)  # DETACHED_PROCESS
    print("설치 완료:", dest)
    print(f"바로가기: 시작프로그램, 바탕화면 '{LNK_NAME[:-4]}'")


def uninstall():
    """바로가기·설치본·설정·저장된 토큰을 지운다. 실행 중인 위젯은 우클릭 › 종료로 먼저 끈다."""
    subprocess.run(["powershell", "-NoProfile", "-Command",
                    "'Startup','Desktop' | % { Remove-Item -LiteralPath "
                    f"([IO.Path]::Combine([Environment]::GetFolderPath($_),'{LNK_NAME}')) "
                    "-ErrorAction SilentlyContinue }"], check=False)
    shutil.rmtree(os.path.join(os.environ["LOCALAPPDATA"], APP), ignore_errors=True)
    shutil.rmtree(CONF_DIR, ignore_errors=True)
    cred_delete()
    print("제거 완료: 바로가기, 설치본, 설정, 저장된 토큰")


# ---------------------------------------------------------------- 진입점

def single_instance():
    ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\" + APP)
    return ctypes.GetLastError() != 183  # ERROR_ALREADY_EXISTS


def main():
    args = sys.argv[1:]
    if "--install" in args:
        install()
        return
    if "--uninstall" in args:
        uninstall()
        return
    global DEMO
    demo = DEMO = "--demo" in args
    if not demo and not single_instance():
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # 고해상도에서 흐리지 않게
    except (AttributeError, OSError):
        pass
    conf = load_conf()
    if demo:
        conf.update(x=100, y=100, topmost=True, alpha=1.0)
    root = tk.Tk()
    root.title("Jira 기한 위젯")
    Widget(root, conf, demo=demo)
    root.mainloop()


if __name__ == "__main__":
    main()
