"""Background-only, idempotent recovery of one room/user's measured intervals."""
import json
import os
import re
import uuid

try:
    from .work_log import read_records, validate_work_samples
except ImportError:
    from work_log import read_records, validate_work_samples


class WorkIntervalTransfer:
    def __init__(self, local_log, user):
        self.current = local_log.room_path
        self.user = str(user)
        self.records = {}
        self.acknowledged = {}
        self.loaded = False
        self.has_pending = True
        self.warnings = []
        self.inflight = set()
        self.recovered_count = 0

    def _load(self):
        parent = self.current.parent.resolve()
        for run in sorted(parent.iterdir()):
            if (not run.is_dir() or not re.fullmatch(r'[a-f0-9]{32}', run.name)
                    or run.resolve().parent != parent):
                continue
            ack_file = run/'work-time-ack.json'
            try:
                ack = json.loads(ack_file.read_text(encoding='utf-8')) if ack_file.is_file() else []
                self.acknowledged[run] = {sid for sid in ack if isinstance(sid, str)} if isinstance(ack, list) else set()
            except (OSError, ValueError):
                self.acknowledged[run] = set()  # Lost ack => retry SAME IDs.
            records, issues = read_records(run)
            self.warnings.extend(issues)
            for record in records:
                if record.get('event') != 'work.interval' or record.get('user') != self.user:
                    continue
                try:
                    sample = validate_work_samples([record['details']])[0]
                except (KeyError, TypeError, ValueError) as exc:
                    self.warnings.append(str(exc))
                    continue
                self.records[sample['sample_id']] = (run, sample)
        self.loaded = True

    def choose(self, current_samples, limit=128):
        if not self.loaded:
            self._load()
        for sample in current_samples:
            self.records[sample['sample_id']] = (self.current, sample)
        self.acknowledged.setdefault(self.current, set())
        already = [s['sample_id'] for s in current_samples
                   if s['sample_id'] in self.acknowledged[self.current]]
        pending = [(sid, run, sample) for sid, (run, sample) in self.records.items()
                   if sid not in self.acknowledged.get(run, set())]
        # Current work must not wait behind an entire historical session.
        recent = [item for item in pending if item[1] == self.current][:limit//2]
        ids = {item[0] for item in recent}
        batch = recent + [item for item in pending if item[0] not in ids][:limit-len(recent)]
        self.inflight = {item[0] for item in batch}
        self.has_pending = bool(pending)
        self.recovered_count = sum(run != self.current for _, run, _ in batch)
        return [item[2] for item in batch], already

    def acknowledge(self, accepted_ids):
        changed = set()
        for sid in set(accepted_ids) & self.inflight:
            run, _ = self.records[sid]
            self.acknowledged.setdefault(run, set()).add(sid)
            changed.add(run)
        for run in changed:
            destination = run/'work-time-ack.json'
            temporary = run/('work-time-ack-'+uuid.uuid4().hex+'.tmp')
            try:
                with temporary.open('w', encoding='utf-8') as stream:
                    json.dump(sorted(self.acknowledged[run]), stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)
        self.inflight.clear()
        self.has_pending = any(sid not in self.acknowledged.get(run, set())
                               for sid, (run, _) in self.records.items())
