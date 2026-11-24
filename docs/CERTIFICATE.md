# 复核监测证据单

从留档打开签发页，确认运行编号、监测烟道和分钟差额后下载一份 JSON。`originals` 中的原件解码成文本后以 UTF-8 重新编码，逐文件计算 SHA-256，应等于 `manifest.sha256`。

核验 `sha256` 时，从附件取出 `manifest`、`originals` 和 `results` 组成对象。深度排序对象键，以 UTF-8 编码 JSON，保留中文原字符，分隔符使用逗号和冒号且省略空白。结果是内容校验摘要。原件可用 `files` 对象重新归档，以 `run_id` 和逐行判据对照两次计算。

按 `results.validity.rows[].sources` 找分钟原件，`mode_sources` 定位工况；按 `results.reports.rows[].reports` 找报送单据。错误版本及 CSV 结构错误分别见 `oxygen.isolated` 和 `intake.files[].rejected`。

留档到期清理后，服务端批次只保留小时结论，不能重新签发。此时仍可把此前下载的完整证据 JSON 提交到 `POST /api/evidence/verify`，或在「监测证据签发」页选择文件/粘贴文本；复核只使用证据单内嵌的 `manifest`、`originals` 和 `results`。「签发并清理」在同一个 SQLite 写事务内先签发、再解除该批引用并剥离库内原件，因此提交成功时返回的证据单一定已经通过摘要和分钟勾稽校验。
