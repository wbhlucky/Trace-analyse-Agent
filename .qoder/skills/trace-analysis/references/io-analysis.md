# I/O Analysis

Require an I/O event, syscall, marker, or blocking interval that overlaps the
affected critical path.

Check:

1. whether the operation is synchronous;
2. which thread issued it;
3. its duration and overlap with the symptom;
4. whether the baseline contains the same operation;
5. whether scheduling or lock wait is a plausible alternative explanation.

Recommend moving work off a critical thread only after identifying that thread
and operation in evidence.
