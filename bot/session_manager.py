import time
import threading
from datetime import datetime, timedelta

class SessionManager:
    def __init__(self):
        self.sessions = {}
        self.lock = threading.Lock()
        self.cleanup_thread = threading.Thread(target=self._cleanup_sessions, daemon=True)
        self.cleanup_thread.start()

    def _cleanup_sessions(self):
        while True:
            time.sleep(300)
            with self.lock:
                current_time = datetime.now()
                expired = [uid for uid, data in self.sessions.items() 
                         if current_time - data['timestamp'] > timedelta(minutes=5)]
                for uid in expired:
                    del self.sessions[uid]

    def save_results(self, user_id, results):
        with self.lock:
            self.sessions[user_id] = {
                'results': results,
                'timestamp': datetime.now()
            }

    def get_results(self, user_id):
        with self.lock:
            session = self.sessions.get(user_id)
            if session and (datetime.now() - session['timestamp']) <= timedelta(minutes=5):
                return session['results']
            return None

    def save_selected_address(self, user_id, address):
        with self.lock:
            if user_id in self.sessions:
                self.sessions[user_id]['selected_address'] = address
                self.sessions[user_id]['timestamp'] = datetime.now()

    def get_selected_address(self, user_id):
        with self.lock:
            session = self.sessions.get(user_id)
            if session and (datetime.now() - session['timestamp']) <= timedelta(minutes=5):
                return session.get('selected_address')
            return None
