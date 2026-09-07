#!/bin/sh
set -eu

# pipefail есть не во всех sh. Если доступен — включаем: без него код возврата
# пайплайна "pg_dump | gzip" равен коду gzip, то есть упавший pg_dump даёт
# успешный пустой архив. Через 14 дней ротация снесла бы последние настоящие
# бэкапы, а мониторинг всё это время показывал бы "всё хорошо".
# shellcheck disable=SC3040
(set -o pipefail 2>/dev/null) && set -o pipefail || HAVE_PIPEFAIL=0

# Файл бэкапа не должен ни на секунду существовать с правами 644.
# chmod после записи оставлял окно, пока идёт дамп.
umask 077

BACKUP_DIR=/backups
RETENTION_DAYS=14
# Пустой gzip-архив — около 20 байт. Порог с большим запасом: дамп даже
# полностью пустой схемы весит килобайты.
MIN_VALID_BYTES=1024

mkdir -p "$BACKUP_DIR"

file_size() {
    # BusyBox stat в alpine поддерживает -c %s
    stat -c %s "$1" 2>/dev/null || echo 0
}

do_backup() {
    STAMP=$(date +%Y%m%d_%H%M%S)
    TARGET="$BACKUP_DIR/feedsift_$STAMP.sql.gz"
    TMP="$TARGET.part"

    echo "backup: pg_dump -> $TARGET"

    if ! PGPASSWORD="$POSTGRES_PASSWORD" pg_dump -h postgres -U "$POSTGRES_USER" "$POSTGRES_DB" \
        | gzip > "$TMP"; then
        echo "backup: ОШИБКА pg_dump/gzip, частичный файл удалён, ротация пропущена" >&2
        rm -f "$TMP"
        return 1
    fi

    SIZE=$(file_size "$TMP")
    if [ "$SIZE" -lt "$MIN_VALID_BYTES" ]; then
        echo "backup: ОШИБКА — архив $SIZE байт (< $MIN_VALID_BYTES), считаем битым" >&2
        rm -f "$TMP"
        return 1
    fi

    # Публикуем под финальным именем только проверенный файл. Ротация ниже
    # смотрит на *.sql.gz, поэтому недоделанный .part под неё не попадёт.
    mv "$TMP" "$TARGET"
    echo "backup: готово, $SIZE байт"

    # Ротация только после успешного бэкапа — иначе серия неудач вычистила бы
    # все рабочие копии.
    find "$BACKUP_DIR" -name '*.sql.gz' -mtime "+$RETENTION_DAYS" -delete
    find "$BACKUP_DIR" -name '*.sql.gz.part' -mtime +1 -delete
    return 0
}

# Разовый запуск: `sh backup.sh once`. Нужен для ручного бэкапа перед деплоем
# или миграцией, и заодно делает скрипт проверяемым без ожидания 04:00.
if [ "${1:-}" = "once" ]; then
    do_backup
    exit $?
fi

while true; do
    NOW_H=$(date +%H)
    NOW_M=$(date +%M)
    if [ "$NOW_H" = "04" ] && [ "$NOW_M" = "00" ]; then
        # Неуспех не должен ронять контейнер: следующая попытка будет завтра,
        # а сообщение об ошибке уже в логах.
        do_backup || echo "backup: попытка провалена, ждём следующего окна" >&2
        sleep 70
    fi
    sleep 30
done
