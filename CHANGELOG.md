# 界面变更

## ui-20261104.2

- 主导航整理为总览、站点、优选效果、记录、设置，保留原路由兼容。
- 站点详情集中展示自然汇报、公开连接检测、域名和站点策略。
- 分别显示汇报失败、连接待观察和证据不足，不相加为误导性的统一异常数。
- 域名表单补充格式和重复提示；保存失败保留输入，新增成功显示新站点。
- 保存值和已报告执行值分别显示，并附执行时间。
- 自动刷新保留策略草稿及关键详情的展开状态；操作控件期间延后重绘。
- 修复状态读取失败时旧的健康提示残留，恢复后清除断线提示。
- 修复空日志反复自动请求；调整无结果及诊断包隐私提示。
- 防止同一站点策略重复提交；离线状态阻止相关保存操作。
- 移除固定部署地址和绝对部署路径，采用系统字体。
- 增加不联网的合成测试。

没有变更后台优选算法、任务调度、站点配置或带宽控制规则；不包含新的传输收益结论。

## 1.10-portable.1

Added standalone backend, Docker Compose, DNS sidecar, expiry guard, safe empty defaults, configurable upstream and UI trust boundary, pinned CFST acquisition, and offline integration tests. NAS-specific active downloader experiments return 501 in this deployment.
