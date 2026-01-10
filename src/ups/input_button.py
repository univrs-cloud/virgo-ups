import logging, os, time, platform, struct, subprocess, sys, typing
from datetime import datetime
from threading import Thread, Lock

# Button event types
BUTTON_PRESS = 1
BUTTON_RELEASE = 2
BUTTON_BLINK = 3

# BLINKING_TOLERANCE - number of seconds to wait for a new pressed/released signal
# before deciding if a "blink" has happened. Increasing this too much delays the
# detection of the release event, consequently when power disconnects the script
# will only detect it after `BLINKING_TOLERANCE` seconds and not one milisecond earlier
BLINKING_TOLERANCE = 4.5  # seconds

# Blink detection threshold - minimum ratio of press/release events to total events
# that indicates blinking behavior (40% of max events for each type)
BLINK_EVENT_THRESHOLD_RATIO = 0.4

ButtonEvent = typing.NamedTuple("ButtonEvent", [("event", int), ("timestamp", datetime)])


"""
v2.0 Test cases, when nearly full battery

Test 1
1. initial status: plugged-in
2. start the script
3. record button status    - pressed
4. unplug + record         - received released event, released
5. plug in + record        - received pressed event, received held event, pressed
6. unplug + record         - received released event, released
7. plug in + record        - received pressed event, received held event, pressed

Test 2
1. initial status: unplugged
2. start the script
3. record button status    - released
4. plug in + record        - received pressed event, received held event, pressed
5. unplug + record         - received released event, released
6. plug in + record        - received pressed event, received held event, pressed
7. unplug + record         - received released event, released
"""


class ConsistentButton(Thread):
    """Button wrapper that provides consistent state tracking and debouncing.
    
    Tracks button events and maintains state, calling callbacks when state changes.
    Useful for GPIO buttons that may have electrical noise or bounce.
    """

    def __init__(self, button: "gpiozero.Button"):
        """Initialize consistent button handler.
        
        Args:
            button: gpiozero.Button instance to wrap
        """
        super().__init__(daemon=True)
        self.button = button
        self.button.when_pressed = self._button_pressed
        self.button.when_released = self._button_released
        self.button.when_held = self._button_held
        self._events = []
        self._max_events = 4
        self.when_pressed = None
        self.when_released = None
        self.when_held = None
        self.running = False
        self._state_lock = Lock()
        self._state = None
        self._previous_state = None

    @property
    def is_pressed(self):
        """Check if button is currently pressed."""
        return self.button.is_pressed

    def run(self):
        """Main thread loop for polling button state."""
        self.running = True
        self._cycle(self.button.is_pressed)

        while self.running:
            try:
                time.sleep(0.5)
                self._cycle(self.button.is_pressed)
            except KeyboardInterrupt:
                self.running = False
                raise

    def stop(self):
        """Stop the monitoring thread and wait for completion."""
        self.running = False
        self.join(timeout=5.0)

    def _cycle(self, current_state):
        """Process button state change.
        
        Args:
            current_state: Current button state (True=pressed, False=released)
        """
        with self._state_lock:
            self._previous_state = self._state
            self._state = current_state

            if self._state == self._previous_state:
                return

            self._do_callbacks(BUTTON_PRESS if self._state else BUTTON_RELEASE)

    def _button_pressed(self):
        """Callback for button press event."""
        self._append_event(BUTTON_PRESS)
        self._cycle(self.button.is_pressed)

    def _button_released(self):
        """Callback for button release event."""
        self._append_event(BUTTON_RELEASE)
        self._cycle(self.button.is_pressed)

    def _button_held(self):
        """Callback for button held event."""
        if self.when_held is not None:
            self.when_held()

    def _append_event(self, event):
        """Append event to history, maintaining max_events size.
        
        Args:
            event: Event type (BUTTON_PRESS, BUTTON_RELEASE, etc.)
        """
        now = datetime.utcnow()
        self._events = [*self._events[1 - self._max_events :], ButtonEvent(event=event, timestamp=now)]

    def _do_callbacks(self, state):
        """Invoke appropriate callback for state change.
        
        Args:
            state: New state (BUTTON_PRESS, BUTTON_RELEASE, or BUTTON_BLINK)
        """
        if state == BUTTON_PRESS and self.when_pressed is not None:
            self.when_pressed()

        elif state == BUTTON_RELEASE and self.when_released is not None:
            self.when_released()

        elif state == BUTTON_BLINK and self.when_blinking is not None:
            self.when_blinking()


class BlinkingButton(ConsistentButton):
    """Button handler that detects blinking patterns.
    
    Extends ConsistentButton to detect rapid press/release cycles (blinking)
    which may indicate power source transitioning between states (e.g., charging).
    Suppresses individual press/release events during blinking and emits a single
    BLINK event instead.
    """

    def __init__(self, button: "gpiozero.Button"):
        """Initialize blinking button handler.
        
        Args:
            button: gpiozero.Button instance to wrap
        """
        super().__init__(button)

        self.when_blinking = None
        self._is_pressed = self.button.is_pressed
        self.blink_duration = BLINKING_TOLERANCE
        self._suppressed_latest_event = False
        self._latest_blink_time = None

    @property
    def is_pressed(self):
        """Check if button is considered pressed (accounting for blinking)."""
        return self._is_pressed

    def _cycle(self, current_state):
        """Process button state with blinking detection.
        
        Args:
            current_state: Current button state (True=pressed, False=released)
        """
        states = {True: BUTTON_PRESS, False: BUTTON_RELEASE}
        if isinstance(current_state, bool):
            current_state = states[current_state]

        now = datetime.utcnow()
        delta = 3600.0  # Default to 1 hour if no events
        if len(self._events) >= 2:
            delta = (now - self._events[-2].timestamp).total_seconds()

        with self._state_lock:
            self._previous_state = self._state
            self._state = current_state

            if self._state == self._previous_state:
                # State unchanged, check if we should release suppressed event
                if self._suppressed_latest_event and delta > self.blink_duration * 1.5:
                    self._is_pressed = current_state == BUTTON_PRESS
                    self._latest_blink_time = None
                    self._suppressed_latest_event = False
                    self._do_callbacks(self._state)

            elif self._check_blinking():
                # Blinking detected - suppress individual events, emit BLINK
                self._suppressed_latest_event = True
                if self._latest_blink_time is None:
                    self._is_pressed = True
                    self._latest_blink_time = now
                    self._suppressed_latest_event = True
                    self._do_callbacks(BUTTON_BLINK)

            elif delta > self.blink_duration * 1.5:
                # Sufficient time passed, treat as real state change
                self._is_pressed = current_state == BUTTON_PRESS
                self._latest_blink_time = None
                self._suppressed_latest_event = False
                self._do_callbacks(current_state)

            else:
                # Too soon after previous event, suppress to avoid noise
                self._suppressed_latest_event = True

    def _check_blinking(self):
        """Check if recent events indicate blinking pattern.
        
        Returns:
            bool: True if blinking pattern detected
        """
        pressed_count = len([e for e in self._events if e.event == BUTTON_PRESS])
        released_count = len([e for e in self._events if e.event == BUTTON_RELEASE])

        threshold = BLINK_EVENT_THRESHOLD_RATIO * self._max_events
        if pressed_count >= threshold and released_count >= threshold:
            latest = self._events[-1]
            oldest = self._events[0]
            delta = latest.timestamp - oldest.timestamp
            if delta.total_seconds() <= self.blink_duration * 2.0:
                return True
        return False
