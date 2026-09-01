# Evals

后续每个评测案例包含：

```text
cases/<case-id>/
├── case.json
├── current.htrace
├── baseline.htrace        # 可选
└── expected.json
```

`expected.json` 应描述：

- 问题类型：冷启动、响应时延、完成时延或帧率/丢帧；
- 必须召回的关键问题；
- 必须引用的证据；
- 允许的根因状态；
- 不允许出现的结论；
- 数值容差。

当前框架只完成文件级数据闭环，接入事件级 HTrace Adapter 后再建立真实评测集。
