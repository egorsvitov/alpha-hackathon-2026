# Итог зоны B

Отчёт составлен оркестратором. Назначенный gpt-6.1-sol / medium остановился из-за лимита Codex; это не отчёт об успешной самостоятельной поставке агента. Сохранённые частичные результаты проверены и доведены оркестратором.

CPU encoder E5-small384 ONNX INT8, shared vector cache, checkpoint/resume, immutable publication, verification/export, incoming/outgoing means. Полная база построена и повторно открыта; graph pilot — 8 target + 206 dependency-only компаний, 3 weighting policies. CUDA FP32 backend не реализован и не проверялся; explicit cuda завершается EncoderUnavailable, auto заранее сообщает CPU fallback.

Команды проверки, версии, фактические измерения и границы готовности: [общий отчёт](company-embeddings-report.md) и [машинный статус](company-embeddings-status.json).
