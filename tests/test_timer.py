import time

import pytest
from buffernotebook import Timer


def test_simple():
    count = 0

    def callback():
        nonlocal count
        count += 1

    t = Timer(callback, 0.1)
    t.event()
    assert count == 0
    time.sleep(0.2)
    assert count == 1


@pytest.mark.parametrize(
    "timer_delay,callback_duration,second_event_delay,expected_count",
    (
        (0.2, 0.1, 0.1, 1),  # second event in timer
        (0.2, 0.1, 0.3, 2),  # second event in callback, timer finishes after callback
        (0.2, 0.3, 0.3, 2),  # second event in callback, timer finishes before callback
    ),
)
def test_complex(timer_delay, callback_duration, second_event_delay, expected_count):
    count = 0

    def callback():
        nonlocal count
        time.sleep(callback_duration)
        count += 1

    t = Timer(callback, timer_delay)
    t.event()
    time.sleep(second_event_delay)
    t.event()
    time.sleep(timer_delay + callback_duration + 0.1)
    assert count == expected_count
