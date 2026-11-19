#!/usr/bin/env bash
# Run the offline test suite and write a results table.
#
#   tests/run_all.sh                    every tests/test_*.py
#   tests/run_all.sh -o DIR             results in DIR (default /tmp/dpmg_run_all)
#   tests/run_all.sh -j 8               run 8 tests at a time
#   tests/run_all.sh -t 600             per-test timeout in seconds (default 300)
#   tests/run_all.sh test_fec_rs.py ... only the named tests
#
# A file that has "def test_" and either imports pytest or has no __main__
# block runs under pytest; every other file runs as a plain script, where
# exit code 0 means pass. Works from any directory.
#
# Output: DIR/results.tsv (name, runner, status, rc, seconds) and one log per
# test in DIR/logs/. The exit code is the number of tests that did not pass
# (capped at 255).
set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
OUT="${DPMG_TEST_OUT:-/tmp/dpmg_run_all}"
JOBS=1
TMO=300
PY="${PYTHON:-python3}"

while getopts "o:j:t:h" opt; do
  case "$opt" in
    o) OUT="$OPTARG" ;;
    j) JOBS="$OPTARG" ;;
    t) TMO="$OPTARG" ;;
    h) sed -n '2,17p' "$0"; exit 0 ;;
    *) exit 2 ;;
  esac
done
shift $((OPTIND - 1))

mkdir -p "$OUT/logs"
OUT="$(cd "$OUT" && pwd)"

if [ $# -gt 0 ]; then
  FILES=()
  for f in "$@"; do FILES+=("$(basename "$f")"); done
else
  FILES=()
  for f in "$ROOT"/tests/test_*.py; do FILES+=("$(basename "$f")"); done
fi

run_one() {
  # $1 = test file name; prints one TSV row
  local name="$1" f="$ROOT/tests/$1" runner start rc status secs
  if grep -q 'def test_' "$f" && { grep -qE '^\s*(import pytest|from pytest)' "$f" \
       || ! grep -qE '__name__ *== *.__main__' "$f"; }; then
    runner=pytest
  else
    runner=script
  fi
  start=$(date +%s.%N)
  (
    cd "$ROOT" || exit 99
    export PYTHONDONTWRITEBYTECODE=1
    if [ "$runner" = pytest ]; then
      exec timeout -k 10 "$TMO" "$PY" -m pytest -q -p no:cacheprovider "tests/$name"
    else
      exec timeout -k 10 "$TMO" "$PY" -B "tests/$name"
    fi
  ) > "$OUT/logs/${name%.py}.log" 2>&1 < /dev/null
  rc=$?
  secs=$(awk -v a="$start" -v b="$(date +%s.%N)" 'BEGIN{printf "%.1f", b-a}')
  if [ $rc -eq 0 ]; then status=PASS
  elif [ $rc -eq 124 ] || [ $rc -eq 137 ]; then status=TIMEOUT
  else status=FAIL; fi
  printf '%s\t%s\t%s\t%s\t%s\n' "$name" "$runner" "$status" "$rc" "$secs"
}
export -f run_one
export ROOT OUT TMO PY

printf 'name\trunner\tstatus\trc\tseconds\n' > "$OUT/results.tsv"
printf '%s\n' "${FILES[@]}" \
  | xargs -P "$JOBS" -I{} bash -c 'run_one "$@"' _ {} \
  | sort >> "$OUT/results.tsv"

npass=$(awk -F'\t' 'NR>1 && $3=="PASS"' "$OUT/results.tsv" | wc -l)
nall=$(( $(wc -l < "$OUT/results.tsv") - 1 ))
echo "$npass/$nall passed; table in $OUT/results.tsv"
awk -F'\t' 'NR>1 && $3!="PASS" {print "  " $3 "  " $1}' "$OUT/results.tsv"
nbad=$((nall - npass))
[ $nbad -gt 255 ] && nbad=255
exit $nbad
