样例 fixture：f fixtures/output.db

这个样例里的 `output.db` 是一个最小化 SQLite 演示文件，
仅用于说明「Case 的 input.trace 指向哪里」。
它不代表真实评测数据。

真实评测时，`output.db` 应是 `trace_streamer` 转换后的 SQLite 数据库，例如：

    D:\Users\Administrator\Desktop\DitingAgent-main\
      results\analyze-cold-start-hiprofiler_data_new-fda512\
      work\job-617ad6b4927d\current.db

取代本样例文件即可，其余 Case / Gold / Trial 结构保持不变。

本演示文件内含以下表，便于快速查询：

- trace_range   1 行   [start_ts=63744938531, end_ts=78309145300]
- callstack     4 行   冷启动生命周期切片
- frame_slice   2 行   首帧相关帧切片
