import logging
import os
import re
import time
from datetime import datetime, timedelta

ARRIVED = "ARRIVED"
INVALID_ID = "INVALID_ID"
NOT_FOUND = "NOT_FOUND"
ALREADY_ARRIVED = "ALREADY_ARRIVED"
FAULT = "FAULT"

log = logging.getLogger("clinic")


class ClinicRules:
    def __init__(self, lead_hours):
        self._lead = timedelta(hours=lead_hours)

    def parse_id(self, text):
        text = (text or "").strip().upper()
        return text if re.fullmatch(r"A-\d{1,6}", text) else None

    def is_due(self, starts_at, now):
        return timedelta(0) < starts_at - now <= self._lead


class AppointmentBook:
    def __init__(self):
        self._rows = {}

    def add(self, appointment_id, clinician_id, patient_id, starts_at):
        self._rows[appointment_id] = [clinician_id, patient_id, starts_at, "booked", False]

    def mark_arrived(self, appointment_id):
        row = self._rows.get(appointment_id)
        if row is None:
            return NOT_FOUND, None
        if row[3] != "booked":
            return ALREADY_ARRIVED, None
        row[3] = "arrived"
        return ARRIVED, row[0]

    def undo_arrival(self, appointment_id):
        self._rows[appointment_id][3] = "booked"

    def awaiting_reminder(self):
        return [(i, r[1], r[2]) for i, r in self._rows.items() if r[3] == "booked" and not r[4]]

    def mark_reminded(self, appointment_id):
        self._rows[appointment_id][4] = True


class MessageOutbox:
    def __init__(self):
        self._sent = []

    def send(self, recipient_id, text):
        self._sent.append((recipient_id, text))

    def messages(self):
        return list(self._sent)


class ClinicService:
    def __init__(self, rules, book, outbox):
        self._rules = rules
        self._book = book
        self._outbox = outbox

    def check_in(self, raw_id):
        appointment_id = self._rules.parse_id(raw_id)
        if appointment_id is None:
            return INVALID_ID
        result, clinician_id = self._book.mark_arrived(appointment_id)
        if result != ARRIVED:
            return result
        try:
            self._outbox.send(clinician_id, f"Patient arrived for {appointment_id}")
        except Exception:
            self._book.undo_arrival(appointment_id)
            log.exception("arrival notice failed for %s", appointment_id)
            return FAULT
        return ARRIVED

    def send_due_reminders(self, now):
        due = [a for a in self._book.awaiting_reminder() if self._rules.is_due(a[2], now)]
        for appointment_id, patient_id, starts_at in due:
            self._outbox.send(patient_id, f"Reminder: {appointment_id} at {starts_at:%H:%M}")
            self._book.mark_reminded(appointment_id)
        return len(due)


def build_clinic():
    book, outbox = AppointmentBook(), MessageOutbox()
    rules = ClinicRules(int(os.environ.get("REMINDER_LEAD_HOURS", "24")))
    return book, outbox, ClinicService(rules, book, outbox)


class BrokenOutbox:
    def send(self, recipient_id, text):
        raise ConnectionError("outbox down")


def run_checks():
    logging.disable(logging.CRITICAL)
    start = datetime(2026, 10, 1, 9, 0)
    lead = timedelta(hours=24)
    rules = ClinicRules(24)

    for raw, expected in [("A-100", "A-100"), ("A-0", "A-0"), (" a-5 ", "A-5"),
                          ("A-1000000", None), ("banana", None), ("", None), (None, None)]:
        assert rules.parse_id(raw) == expected
    assert not rules.is_due(start, start - lead - timedelta(minutes=1))
    assert rules.is_due(start, start - lead)
    assert rules.is_due(start, start - timedelta(minutes=1))
    assert not rules.is_due(start, start)

    book, outbox, service = build_clinic()
    book.add("A-1", "DR-1", "P-77", start)
    assert service.check_in("A-1") == ARRIVED
    assert service.check_in("A-1") == ALREADY_ARRIVED
    assert service.check_in("A-404") == NOT_FOUND
    assert service.check_in("banana") == INVALID_ID
    assert outbox.messages() == [("DR-1", "Patient arrived for A-1")]

    book = AppointmentBook()
    book.add("A-2", "DR-1", "P-88", start)
    assert ClinicService(rules, book, BrokenOutbox()).check_in("A-2") == FAULT
    assert ClinicService(rules, book, MessageOutbox()).check_in("A-2") == ARRIVED

    book, outbox, service = build_clinic()
    book.add("A-3", "DR-1", "P-99", start)
    assert service.send_due_reminders(start - lead - timedelta(minutes=1)) == 0
    assert service.send_due_reminders(start - lead) == 1
    assert service.send_due_reminders(start - lead) == 0

    book, outbox, service = build_clinic()
    for n in range(5000):
        book.add(f"A-{n}", f"DR-{n % 200}", f"P-{n}", start)
    times = []
    for n in range(1000):
        began = time.perf_counter()
        service.check_in(f"A-{n}")
        times.append(time.perf_counter() - began)
    times.sort()
    assert times[949] < 1
    return times[949] * 1000


if __name__ == "__main__":
    print(f"All checks passed. Check-in p95 = {run_checks():.3f} ms")
