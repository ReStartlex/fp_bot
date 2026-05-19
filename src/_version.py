"""
Версия задеплоенного кода. Записывается автоматически скриптом
deploy/stamp_version.py перед каждым push'ем.

ВАЖНО: НЕ редактируй вручную — твои изменения будут перезаписаны.
"""
SHA = "223796563dc7ee9ca3308d6e485fa4eac499b3d7"
DATE = "2026-05-19T13:12:42+03:00"
SUBJECT = "fix(watcher): register message under BOTH id-key and text-key (single source might have id, other might not -> dedup must intersect in either dimension; fixes duplicate help-ack/sos when one msg seen "
