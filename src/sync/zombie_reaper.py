"""
Zombie-lot reaper: устранение half-disabled state.

Сценарий, который реактивно решается этим модулем:

  1. Заказ failed (NS не выдал товар);
  2. _emergency_disable_lot отключает mapping в БД;
  3. ОДНОВРЕМЕННО пытается save_lot(active=False) на FunPay;
  4. Этот save_lot падает (например, 429 burst);
  5. Получается рассогласование:
       * mapping.enabled = False (sync_stock игнорирует);
       * FunPay лот active=True (продаётся со старым stock'ом).
  6. Покупатели продолжают делать заказы → каждый из них тоже failed.

Reaper периодически проверяет disabled-маппинги и, если на FunPay лот
всё ещё активен — пытается deactivate его снова. Это идемпотентно:
если лот уже deactivated, save_lot не делается.

Алгоритм:
  - читаем mappings WHERE enabled=False;
  - для каждого: GET FunPay lot_fields;
    * если active=False → пропускаем (уже dead);
    * иначе save_lot(active=False) — amount НЕ трогаем (FunPay
      отбраковывает форму с amount=0, см. инцидент 2026-06);
  - не больше max_per_run save_lot за один прогон (защита от 429 burst);
  - метрики через ReaperResult, Telegram-уведомление при успешной reap.

Anti-spam: уведомление оператору — не чаще одного раза на «эпизод зомби»
(пока лот не подтверждён dead). Состояние хранится в БД
(mappings.zombie_reaper_notified_at), переживает рестарт.

Запускается через APScheduler-job `zombie_lot_reaper` (default 600с).
Отдельно от sync_stock, потому что:
  - sync_stock фильтрует enabled=True (своя забота);
  - reaper работает только с enabled=False — разная семантика;
  - интервал другой (10 мин vs 30с).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable

from loguru import logger
from sqlalchemy import select

from src.config import Settings
from src.db.models import Mapping
from src.db.repo import (
    clear_zombie_reaper_notified,
    mark_zombie_reaper_notified,
)
from src.db.session import session_factory
from src.funpay.client import FunPayClient


@dataclass
class ReaperResult:
    """Метрики одного прогона zombie reaper'а."""
    # Сколько disabled-маппингов посмотрели (limit max_per_run).
    checked: int = 0
    # FunPay лот уже active=False — ничего не делаем.
    already_dead: int = 0
    # Сделали save_lot(active=False) успешно (и verify подтвердил).
    deactivated: int = 0
    # FunPay GET или save_lot упал.
    errors: int = 0
    # Уведомление оператору подавлено (уже уведомляли в этом эпизоде).
    notify_suppressed: int = 0

    @property
    def reaped(self) -> bool:
        """True, если хотя бы один лот был успешно deactivated в этом прогоне."""
        return self.deactivated > 0


# Тип callback'а для уведомления оператора. Принимает текст HTML-сообщения.
NotifyOwnerFn = Callable[[str], Awaitable[None]]


def _is_lot_already_dead(lot_fields: object) -> bool:
    """
    Проверка: лот на FunPay уже в состоянии «deactivated»?

    Dead = active=False (или поле отсутствует). Amount НЕ проверяем:
    FunPay не принимает amount=0, поэтому у выключенного лота остаётся
    старое количество — это нормально. Раньше требование amount==0
    делало состояние «dead» недостижимым, и reaper бесконечно
    «деактивировал» один и тот же лот каждые 10 минут.
    """
    return not bool(getattr(lot_fields, "active", False))


def _set_lot_dead(lot_fields: object) -> None:
    """Выставляет active=False на объекте lot_fields (in-place).

    Amount НЕ трогаем: форма с amount=0 молча отбраковывается FunPay,
    и save «успешно» ничего не меняет (см. инцидент 2026-06).
    """
    if hasattr(lot_fields, "active"):
        lot_fields.active = False


async def _persist_zombie_notified(mapping_id: int) -> None:
    async with session_factory()() as session:
        await mark_zombie_reaper_notified(session, mapping_id=mapping_id)
        await session.commit()


async def _persist_zombie_cleared(mapping_id: int) -> None:
    async with session_factory()() as session:
        await clear_zombie_reaper_notified(session, mapping_id=mapping_id)
        await session.commit()


async def reap_zombie_lots_once(
    *,
    funpay_client: FunPayClient,
    settings: Settings,
    notify_owner: NotifyOwnerFn | None = None,
) -> ReaperResult:
    """
    Один прогон zombie-reaper'а: deactivate'ит зависшие в half-disabled
    состоянии лоты на FunPay.

    Параметры:
        funpay_client: подключённый FunPayClient (если None, прогон
            пропускается — нет смысла собирать disabled-маппинги без
            возможности их fix'нуть).
        settings: глобальный конфиг (берём max_per_run, проверяем
            enable_real_actions для dry-run режима).
        notify_owner: опциональный callback для Telegram-уведомления
            при успешной reap. Если None — не уведомляем (полезно
            для unit-тестов).

    Возвращает ReaperResult с метриками.

    Никогда не бросает исключения наружу — все ошибки логируются и
    учитываются в .errors. APScheduler-job не должен падать из-за
    одного зомби-лота.
    """
    result = ReaperResult()
    max_per_run = settings.zombie_lot_reaper_max_per_run

    # 1. Собираем кандидатов: disabled-маппинги, которые МОГУТ быть
    #    половинчато-выключенными зомби (на FunPay ещё active).
    #
    #    Фильтры (инцидент id=49 / удалённый лот 69932320):
    #      * funpay_lot_id > 0 — отсекает sentinel-0 и мусорные строки
    #        (GET lot 0 падает «обязателен node_id», GET удалённого —
    #        бесконечно валится в errors);
    #      * last_synced_active IS NOT 0 — если мы УЖЕ зафиксировали лот
    #        неактивным (или он удалён), зомби нет, проверять незачем.
    #        Настоящий зомби рождается из ПРОВАЛЕННОГО save_lot(active=False)
    #        → last_synced НЕ обновился → остаётся 1/NULL → берём как раньше.
    #      enabled=False оставляем: это и есть мишень reaper'а (НЕ менять
    #      на enabled=True — иначе reaper перестанет чинить зомби).
    try:
        async with session_factory()() as session:
            stmt = (
                select(Mapping)
                .where(Mapping.enabled.is_(False))
                .where(Mapping.funpay_lot_id > 0)
                .where(Mapping.last_synced_active.isnot(False))
                .order_by(Mapping.id)
                .limit(max_per_run)
            )
            mappings = list((await session.execute(stmt)).scalars().all())
    except Exception as exc:
        logger.opt(exception=exc).error(
            f"zombie reaper: ошибка чтения disabled-маппингов: {exc}"
        )
        result.errors += 1
        return result

    if not mappings:
        return result

    # 2. Для каждого: GET → возможно save_lot.
    notify_labels: list[str] = []
    for mapping in mappings:
        result.checked += 1
        lot_id = mapping.funpay_lot_id
        label = mapping.label or f"lot {lot_id}"
        already_notified = mapping.zombie_reaper_notified_at is not None

        try:
            lot_fields = await funpay_client.get_lot_fields(lot_id)
        except Exception as exc:
            logger.warning(
                f"zombie reaper: GET lot {lot_id} ({label}) упал: {exc}"
            )
            result.errors += 1
            continue

        if _is_lot_already_dead(lot_fields):
            logger.debug(
                f"zombie reaper: lot {lot_id} ({label}) уже dead, skip"
            )
            result.already_dead += 1
            if already_notified:
                try:
                    await _persist_zombie_cleared(mapping.id)
                except Exception as exc:
                    logger.warning(
                        f"zombie reaper: не удалось сбросить notified_at "
                        f"для mapping {mapping.id}: {exc}"
                    )
            continue

        # Лот active — надо deactivate.
        if not settings.enable_real_actions:
            logger.info(
                f"zombie reaper: DRY-RUN lot {lot_id} ({label}) "
                f"will be deactivated (active=False)"
            )
            result.deactivated += 1
            if not already_notified:
                notify_labels.append(f"#{lot_id} {label}")
                try:
                    await _persist_zombie_notified(mapping.id)
                except Exception as exc:
                    logger.warning(
                        f"zombie reaper: не удалось записать notified_at "
                        f"для mapping {mapping.id}: {exc}"
                    )
            else:
                result.notify_suppressed += 1
            continue

        try:
            _set_lot_dead(lot_fields)
            save_result = await funpay_client.save_lot(lot_fields)
            if isinstance(save_result, dict) and save_result.get("ok") is False:
                raise RuntimeError(
                    f"save_lot вернул ok=False: {save_result.get('funpay_error') or save_result}"
                )
            # Verify-after-save: FunPay умеет ответить «успехом», не
            # применив форму. Без этой проверки reaper часами логировал
            # SUCCESS по лоту, который так и оставался активным.
            verify = await funpay_client.get_lot_fields(lot_id)
            if not _is_lot_already_dead(verify):
                raise RuntimeError(
                    "save_lot отчитался успехом, но лот на FunPay "
                    "всё ещё активен (деактивация не применилась)"
                )
        except Exception as exc:
            logger.warning(
                f"zombie reaper: save_lot для {lot_id} ({label}) упал: {exc}"
            )
            result.errors += 1
            continue

        logger.success(
            f"zombie reaper: lot {lot_id} ({label}) deactivated "
            f"(half-disabled state устранён)"
        )
        result.deactivated += 1
        if not already_notified:
            notify_labels.append(f"#{lot_id} {label}")
            try:
                await _persist_zombie_notified(mapping.id)
            except Exception as exc:
                logger.warning(
                    f"zombie reaper: не удалось записать notified_at "
                    f"для mapping {mapping.id}: {exc}"
                )
        else:
            result.notify_suppressed += 1

    # 3. Уведомление оператору — только для лотов, по которым ещё не
    # уведомляли в текущем «эпизоде зомби».
    if notify_labels and notify_owner is not None:
        lots_text = "\n  ".join(notify_labels[:10])
        more = (
            f"\n…и ещё {len(notify_labels) - 10}"
            if len(notify_labels) > 10 else ""
        )
        try:
            await notify_owner(
                f"🧟 <b>Zombie-reaper deactivated {len(notify_labels)} лот(ов):</b>\n  "
                f"{lots_text}{more}\n\n"
                "Это были disabled-маппинги, у которых лот на FunPay был "
                "ещё активен после failed-заказа. Теперь они сняты с продажи. "
                "Если хочешь вернуть в продажу — /mappings → 🟢 для нужного."
            )
        except Exception as exc:
            logger.warning(f"zombie reaper: notify_owner упал: {exc}")

    return result
