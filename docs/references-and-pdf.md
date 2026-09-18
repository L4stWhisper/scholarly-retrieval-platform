# Reference 提取与本地 PDF

系统不会自动下载论文 PDF。检索结果可能包含 `pdf_url`；用户或 Agent 应根据许可和任务需要显式决定
是否获取全文。对已经合法取得的文件，可以在本地抽取参考文献并链接到论文实体。

## 抽取

```bash
# JATS、TEI、LaTeX、BibTeX 文本
scholar extract-references article.xml --input-format jats

# born-digital PDF，需要用户运行 GROBID
scholar extract-references article.pdf --input-format pdf \
  --grobid-url http://127.0.0.1:8070

# 扫描/混合 PDF，先本地 OCRmyPDF，再进入 GROBID
scholar extract-references scan.pdf --input-format pdf --ocr \
  --ocr-language eng+chi_sim --grobid-url http://127.0.0.1:8070
```

Python OCR 依赖用 `uv sync --extra ocr` 安装，仍需本机 OCRmyPDF、Tesseract 与 GROBID 运行时；
`compose.ocr.yaml` 提供可复现的容器环境，需要至少约 4 GiB 内存。

## 链接到论文实体

```bash
scholar link-references "10.1038/s41586-021-03819-2" extracted.json --source openalex,crossref
```

第一个参数是被抽取文档自身的标识（seed），第二个参数是 `extract-references` 的 JSON 输出。

候选按 DOI 优先，再用题名、作者、venue、年份分项评分；只有得分超过
`SCHOLAR_REFERENCE_AUTO_MATCH_THRESHOLD` 且与第二名分差超过 `SCHOLAR_REFERENCE_MINIMUM_MARGIN` 时
才自动匹配，模糊情况保留为未解析书目而不是伪造引文边。

## 安全边界

PDF 输入/输出上限为 100 MiB。生产环境应把 PDF 当作不可信输入，在隔离、限时、限 CPU/内存的
worker 中处理，不得绕过付费墙、验证码或访问控制。
