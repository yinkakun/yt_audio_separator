import time


class CleanupManager:
    def __init__(self):
        self.last_cleanup_time = 0

    def should_cleanup(self) -> bool:
        current_time = time.time()
        time_limit_seconds = 60 * 5
        if current_time - self.last_cleanup_time > time_limit_seconds:
            self.last_cleanup_time = current_time
            return True
        return False
