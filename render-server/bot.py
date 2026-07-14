import asyncio
import concurrent.futures
import json
import os
import time
import urllib.request
from datetime import datetime, timezone, timedelta

KST = timezone(timedelta(hours=9))

sessions: dict = {}


def _fmt_time(t) -> str:
    if t is None:
        return ""
    if hasattr(t, "strftime"):
        return t.strftime("%H%M%S")
    s = str(t).replace(":", "").replace(" ", "")
    digits = "".join(c for c in s if c.isdigit())
    return digits[-6:] if len(digits) >= 6 else digits


class BotSession:
    def __init__(self, session_id: str, settings: dict):
        self.session_id = session_id
        self.settings = settings
        self.status = "idle"
        self.logs: list[str] = []
        self.attempt = 0
        self.last_train = None
        self._running = False
        self._end_datetime = None 

    def _log(self, msg: str):
        ts = datetime.now(KST).strftime("%H:%M:%S")       
        self.logs.insert(0, f"[{ts}] {msg}")
        self.logs = self.logs[:100]

    def stop(self):
        self._running = False

    def get_status(self) -> dict:
        return {
            "status": self.status,
            "logs": self.logs,
            "attempt": self.attempt,
            "lastTrain": self.last_train,
        }

    def _is_after_end_time(self) -> bool:
        return datetime.now(KST) >= self._end_datetime

    def _run_sync(self):
        from SRT import SRT, SeatType

        s = self.settings
        dep_time = str(s["depTime"]).zfill(2) + str(s["depMinute"]).zfill(2) + "00"
        end_time = str(s["endTime"]).zfill(2) + str(s["endMinute"]).zfill(2) + "00"
        date = s["date"].replace("-", "")
        interval_sec = max(0.5, int(s.get("intervalMs", 1000)) / 1000)

        now = datetime.now(KST)
        end_dt = now.replace(hour=int(s["endTime"]), minute=int(s["endMinute"]), second=0, microsecond=0)
        if end_dt <= now:
            end_dt += timedelta(days=1)
        self._end_datetime = end_dt

        self._log(f"로그인 시도 ({s['depStation']} → {s['arrStation']})")

        try:
            srt = SRT(s["userId"], s["password"])
            self._log("로그인 성공")
            self.status = "running"
        except Exception as e:
            self._log(f"로그인 실패: {e}")
            self.status = "error"
            self._running = False
            return

        while self._running:
            if self._is_after_end_time():
                end_h = str(s["endTime"]).zfill(2)
                end_m = str(s["endMinute"]).zfill(2)
                self._log(f"종료 시간 {end_h}:{end_m} 도달 → 자동 중지")
                self.status = "idle"
                self._running = False
                break

            try:
                trains = srt.search_train(
                    s["depStation"],
                    s["arrStation"],
                    date,
                    dep_time,
                    end_time,
                    available_only=True,
                )

                self.attempt += 1
                if self.attempt % 10 == 0:
                    self._log(f"{self.attempt}회 탐색 중...")
    
                if trains:
                    train = trains[0]
                    dep_str = _fmt_time(train.dep_time)
                    arr_str = _fmt_time(train.arr_time)
                    self.last_train = {"dptTm": dep_str, "arvTm": arr_str}
                    self._log(
                        f"예약 가능 열차 발견: "
                        f"{dep_str[:2]}:{dep_str[2:4]} → {arr_str[:2]}:{arr_str[2:4]}"
                    )

                    try:
                        srt.reserve(train, special_seat=SeatType.GENERAL_ONLY)
                        self.status = "success"
                        self._running = False
                        self._log("예약 완료!")
                        if s.get("fcmToken"):
                            self._send_fcm_sync(s["fcmToken"], dep_str, arr_str)
                        break
                    except Exception as e:
                        self._log(f"예약 시도 실패: {e}")

            except Exception as e:
                err = str(e)
                self._log(f"오류: {err}")
                if "로그인" in err or "login" in err.lower() or "session" in err.lower():
                    self._log("세션 만료. 재로그인 시도...")
                    try:
                        srt = SRT(s["userId"], s["password"])
                        self._log("재로그인 성공")
                    except Exception as le:
                        self._log(f"재로그인 실패: {le}")
                        self.status = "error"
                        self._running = False
                        break

            if self._running:
                time.sleep(interval_sec)

    def _send_fcm_sync(self, fcm_token: str, dep_str: str, arr_str: str):
        from google.oauth2 import service_account
        import google.auth.transport.requests

        sa_json = os.environ.get("FCM_SERVICE_ACCOUNT_JSON", "")
        if not sa_json:
            self._log("FCM_SERVICE_ACCOUNT_JSON 환경변수 미설정 — FCM 알림 생략")
            return

        try:
            sa_info = json.loads(sa_json)
            project_id = sa_info.get("project_id", "")
            credentials = service_account.Credentials.from_service_account_info(
                sa_info,
                scopes=["https://www.googleapis.com/auth/firebase.messaging"],
            )
            credentials.refresh(google.auth.transport.requests.Request())
            access_token = credentials.token
        except Exception as e:
            self._log(f"FCM 인증 실패: {e}")
            return

        s = self.settings
        dep_fmt = f"{dep_str[:2]}:{dep_str[2:4]}" if len(dep_str) >= 4 else ""
        arr_fmt = f"{arr_str[:2]}:{arr_str[2:4]}" if len(arr_str) >= 4 else ""
        body = f"{s['depStation']} → {s['arrStation']}"
        if dep_fmt:
            body += f"  출발 {dep_fmt} → 도착 {arr_fmt}"
        body += "\n10분 내 SRT 앱에서 결제하세요!"

        payload = json.dumps({
            "message": {
                "token": fcm_token,
                "notification": {
                    "title": "SRT 예약 완료!",
                    "body": body,
                },
                "android": {
                    "priority": "high",
                    "notification": {"sound": "default"},
                },
            }
        }).encode("utf-8")

        req = urllib.request.Request(
            f"https://fcm.googleapis.com/v1/projects/{project_id}/messages:send",
            data=payload,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            urllib.request.urlopen(req, timeout=10)
            self._log("FCM 알림 전송 완료")
        except Exception as e:
            self._log(f"FCM 전송 실패: {e}")

    async def run(self):
        self._running = True
        self.status = "logging_in"
        loop = asyncio.get_event_loop()
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            await loop.run_in_executor(executor, self._run_sync)
        finally:
            executor.shutdown(wait=False)
