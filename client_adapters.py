# -*- coding: utf-8 -*-
import glob
import os
import re


class AdapterError(Exception):
    pass


class FixtureAdapter:
    name = "fixture"
    context_dirs = ()

    def __init__(self, environ):
        self.environ = environ
        self.path = environ.get("TOKENSCOST_TEST_TRANSCRIPT")

    def session_id(self):
        return "test"

    def find_transcript(self):
        if self.path and os.path.exists(self.path):
            return os.path.abspath(self.path)
        raise AdapterError("测试 transcript 不存在：%s" % self.path)


class WorkBuddyAdapter:
    name = "workbuddy"
    context_dirs = (".workbuddy", ".codebuddy")

    def __init__(self, environ):
        self.environ = environ

    def session_id(self):
        session_id = self.environ.get("CODEBUDDY_SESSION_ID")
        if not session_id or not re.fullmatch(r"[0-9A-Za-z_-]+", session_id):
            raise AdapterError("缺少有效 CODEBUDDY_SESSION_ID，无法绑定当前会话")
        return session_id

    def find_transcript(self):
        session_id = self.session_id()
        home = os.path.expanduser("~")
        matches = []
        for root_name in self.context_dirs:
            root = os.path.join(home, root_name, "projects")
            matches.extend(glob.glob(os.path.join(root, "**", session_id + ".jsonl"),
                                     recursive=True))
        matches = sorted({os.path.abspath(path) for path in matches})
        if len(matches) != 1:
            raise AdapterError("当前会话 transcript 匹配数为 %d，不能选择其他会话代替"
                               % len(matches))
        return matches[0]


def detect_adapter(environ=None):
    environ = environ or os.environ
    if environ.get("TOKENSCOST_TEST_TRANSCRIPT"):
        return FixtureAdapter(environ)
    requested = (environ.get("TOKENSCOST_CLIENT") or "").lower()
    if requested in ("workbuddy", "codebuddy") or environ.get("CODEBUDDY_SESSION_ID"):
        return WorkBuddyAdapter(environ)
    if requested:
        raise AdapterError("未内置客户端 %s 的会话适配器；不能猜测 transcript 或 credit 来源"
                           % requested)
    raise AdapterError("未识别当前 agent 客户端；目前可自动适配 WorkBuddy 桌面版/CLI，"
                       "其他客户端需要对应适配器，不能扫描或借用其他会话")
