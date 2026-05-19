"""
Версия задеплоенного кода. Записывается автоматически скриптом
deploy/stamp_version.py перед каждым push'ем.

ВАЖНО: НЕ редактируй вручную — твои изменения будут перезаписаны.
"""
SHA = "b762bd13eb1161a8bf639aac515c513bfc177098"
DATE = "2026-05-19T12:51:45+03:00"
SUBJECT = "fix(watcher,chat): filter self-messages by username (FunPay HTML often misses data-author -> bot was triggering on its own greeting that contained '!РїРѕРјРѕС‰СЊ'; rewords templates to remove trigger "
