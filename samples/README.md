# 放你过去 3-5 篇文章到这里

Agent D0 (style learner) 会读这里所有 .md / .docx / .txt 文件，
分析你的语言指纹 / 句式偏好 / 禁用词，合成 style_profile.yaml。

支持格式：
- .md   (Markdown)
- .txt  (纯文本)
- .docx (Word)

建议：
- 挑你觉得"最像自己"的 3-5 篇
- 全文即可，不用裁剪
- 多样性比数量重要（博客 1-2 篇 + 长推 1-2 篇 + newsletter 1 篇 最佳）

准备好了跑：
  cd backend
  source .venv/bin/activate
  af learn-style --dir ./samples/
  af learn-style --show   # 查看学到的 profile
