"""Small, dependency-free UI language packs for etm."""

LANGUAGES = {
    "en": "English", "zh": "简体中文", "ja": "日本語", "ko": "한국어",
    "es": "Español", "fr": "Français", "de": "Deutsch",
}

MESSAGES = {
    "en": {
        "app": "etm · Concurrent streaming answers (Ctrl-C to cancel; full Markdown follows)",
        "waiting": "Waiting for response…", "connecting": "Connecting", "answering": "Answering",
        "complete": "Complete", "failed": "Failed", "synthesizing": "Synthesizing",
        "synthesis": "Synthesis",
        "preview": "Live Markdown preview", "no_content": "_No content returned_",
        "missing_question": "Please provide a question, e.g. `etm what is TAM SAM SOM`.",
        "cancelled": "Cancelled.", "api_key_status": "API key",
        "api_key_set": "found in environment or Keychain", "api_key_missing": "not configured",
        "language_updated": "Interface language set to {name} ({code}).",
        "language_invalid": "Unsupported language {code!r}. Available: {available}",
    },
    "zh": {
        "app": "etm · 并发流式回答（Ctrl-C 取消；完成后显示完整 Markdown）",
        "waiting": "等待回复…", "connecting": "连接中", "answering": "回答中", "complete": "完成", "failed": "失败", "synthesizing": "综合中",
        "synthesis": "综合",
        "preview": "实时 Markdown 预览", "no_content": "_没有返回内容_", "missing_question": "请提供问题，例如：`etm what is TAM SAM SOM`。", "cancelled": "已取消。",
        "api_key_status": "API key", "api_key_set": "已在环境变量或 Keychain 中找到", "api_key_missing": "未设置",
        "language_updated": "界面语言已设为 {name}（{code}）。", "language_invalid": "不支持语言 {code!r}。可用语言：{available}",
    },
    "ja": {"app": "etm · 同時ストリーミング回答（Ctrl-C で中止；完了後に完全な Markdown を表示）", "waiting": "応答待ち…", "connecting": "接続中", "answering": "回答中", "complete": "完了", "failed": "失敗", "synthesizing": "統合中", "synthesis": "統合", "preview": "ライブ Markdown プレビュー", "no_content": "_内容は返されませんでした_"},
    "ko": {"app": "etm · 동시 스트리밍 답변(Ctrl-C로 취소; 완료 후 전체 Markdown 표시)", "waiting": "응답 대기 중…", "connecting": "연결 중", "answering": "답변 중", "complete": "완료", "failed": "실패", "synthesizing": "종합 중", "synthesis": "종합", "preview": "실시간 Markdown 미리보기", "no_content": "_반환된 내용 없음_"},
    "es": {"app": "etm · Respuestas simultáneas en streaming (Ctrl-C para cancelar; Markdown completo al final)", "waiting": "Esperando respuesta…", "connecting": "Conectando", "answering": "Respondiendo", "complete": "Completado", "failed": "Falló", "synthesizing": "Sintetizando", "synthesis": "Síntesis", "preview": "Vista previa Markdown en vivo", "no_content": "_No se devolvió contenido_"},
    "fr": {"app": "etm · Réponses simultanées en flux (Ctrl-C pour annuler ; Markdown complet à la fin)", "waiting": "En attente de réponse…", "connecting": "Connexion", "answering": "Réponse en cours", "complete": "Terminé", "failed": "Échec", "synthesizing": "Synthèse", "synthesis": "Synthèse", "preview": "Aperçu Markdown en direct", "no_content": "_Aucun contenu renvoyé_"},
    "de": {"app": "etm · Gleichzeitige Streaming-Antworten (Strg-C zum Abbrechen; vollständiges Markdown danach)", "waiting": "Warte auf Antwort…", "connecting": "Verbinden", "answering": "Antwortet", "complete": "Fertig", "failed": "Fehlgeschlagen", "synthesizing": "Zusammenfassen", "synthesis": "Zusammenfassung", "preview": "Live-Markdown-Vorschau", "no_content": "_Kein Inhalt zurückgegeben_"},
}


def tr(language: str, key: str, **values: str) -> str:
    """Return English for incomplete language packs, keeping new packs easy to add."""
    return MESSAGES.get(language, {}).get(key, MESSAGES["en"][key]).format(**values)
