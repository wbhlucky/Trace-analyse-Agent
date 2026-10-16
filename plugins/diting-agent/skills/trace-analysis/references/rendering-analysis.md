# Rendering Analysis

Require frame, render-thread, GPU, or presentation evidence before assigning a
rendering root cause.

Distinguish application UI work, render submission, GPU execution, compositor
work, and final presentation. A slow application frame does not by itself prove
a GPU bottleneck. Compare each affected stage against the actual frame budget
and report missing tracks as limitations.
