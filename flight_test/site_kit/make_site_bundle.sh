#!/usr/bin/env bash
# make_site_bundle.sh: build the site deploy bundle for the payload G40 from one commit of
# the flight copy. Laptop only: nothing here contacts a radio, the 8080 GUI or Docker.
#
#   bash make_site_bundle.sh                 # HEAD of this flight copy
#   bash make_site_bundle.sh <commit>        # any commit, branch or tag
#
# Writes bundle/<short id>/ beside this script and points bundle/CURRENT at it:
#   dpmg_flight_<id>.tar.gz   git archive of the commit (only committed files, no
#                             rf_frontend.yaml, no data/) plus native/lib/librxdrain.so,
#                             which git ignores and which is added from NATIVE_LIB
#   SHA256SUMS                the archive, the drain library and every file of the deploy set
#   BUNDLE_INFO.txt           commit id, subject, date, archive size and SHA256
#   DEPLOY_SET.txt            deploy_files.txt checked against the commit, with full hashes
#   RADIO_FILES.txt           every radio-side file to hash on the unit before and after
#   REFERENCE_PAYLOAD.sha256     the payload unit's expected code (5 November), sha256sum format
#   RADIO_DIFF.txt            each radio-side file: reference against this commit
#
# The reference for the payload unit's code is the full tree snapshot of 5 November 18:38
# (taken after the unit's last deploy at 09:09 that day), given as REF_TGZ; without it the
# stored hashes in REFERENCE_PAYLOAD.sha256 beside this script are used. The same
# commit gives the same archive SHA256 on the same laptop (gzip -n, git archive times
# come from the commit).
#
# The native receive drain (native/lib/librxdrain.so, aarch64) gives 0 overruns at 61.44 MS/s
# on the G40; without it the service falls back to the Python drain. It is not in git, so it
# is taken from NATIVE_LIB (default the flight copy's native/lib) and must be an aarch64 ELF
# whose SHA256 starts with NATIVE_SHA (default de8d9a2440830f3b, the build running on
# the bench G40 and in the 5 November snapshot).
set -euo pipefail
KIT="$(cd "$(dirname "$0")" && pwd)"
REPO="${REPO:-$(cd "$KIT/../.." && pwd)}"
REF_TGZ="${REF_TGZ:-}"
REV="${1:-HEAD}"
NATIVE_LIB="${NATIVE_LIB:-$REPO/native/lib/librxdrain.so}"
NATIVE_SHA="${NATIVE_SHA:-de8d9a2440830f3b}"
NATIVE_REL=native/lib/librxdrain.so

FULL=$(git -C "$REPO" rev-parse --verify "$REV^{commit}")
SHORT=${FULL:0:7}
NAME="dpmg_flight_$SHORT"
OUT="$KIT/bundle/$SHORT"
mkdir -p "$OUT"
TMP=$(mktemp -d "${TMPDIR:-/tmp}/site_bundle.XXXXXX")
trap 'rm -rf "$TMP"' EXIT

echo "== commit $FULL"
git -C "$REPO" log -1 --format='   %s (%ci)' "$FULL"
if [ "$REV" = HEAD ] && [ -n "$(git -C "$REPO" status --porcelain --untracked-files=no)" ]; then
  echo "   note: the checkout has uncommitted edits; they are NOT in the bundle"
fi

echo "== native drain library"
[ -f "$NATIVE_LIB" ] || { echo "   $NATIVE_LIB not found (native/build.sh on a G40 builds it)"; exit 1; }
NSHA=$(sha256sum "$NATIVE_LIB" | cut -d' ' -f1)
[ "$(od -An -tx1 -j18 -N2 "$NATIVE_LIB" | tr -d ' ')" = b700 ] \
  || { echo "   $NATIVE_LIB is not an aarch64 library"; exit 1; }
case "$NSHA" in "$NATIVE_SHA"*) ;; *) echo "   $NATIVE_LIB sha256 $NSHA does not start with $NATIVE_SHA"; exit 1 ;; esac
echo "   $NATIVE_LIB  $(stat -c %s "$NATIVE_LIB") bytes  sha256 $NSHA"

echo "== archive"
# --add-file takes the --prefix given before it; the tree takes the last one
git -C "$REPO" archive --format=tar --prefix="$NAME/$(dirname "$NATIVE_REL")/" --add-file="$NATIVE_LIB" \
  --prefix="$NAME/" "$FULL" | gzip -n -9 > "$OUT/$NAME.tar.gz"
ASHA=$(sha256sum "$OUT/$NAME.tar.gz" | cut -d' ' -f1)
ASIZE=$(stat -c %s "$OUT/$NAME.tar.gz")
echo "   $NAME.tar.gz  $ASIZE bytes  sha256 $ASHA"
mkdir -p "$TMP/new" && tar xzf "$OUT/$NAME.tar.gz" -C "$TMP/new"
NEW="$TMP/new/$NAME"
[ "$(sha256sum "$NEW/$NATIVE_REL" | cut -d' ' -f1)" = "$NSHA" ] \
  || { echo "   $NATIVE_REL in the archive does not match $NATIVE_LIB"; exit 1; }

echo "== deploy set"
: > "$OUT/DEPLOY_SET.txt"
DEPLOY=(); EXCL=()
while IFS= read -r line; do
  line="${line%%#*}"; line="$(echo "$line" | xargs)"
  [ -n "$line" ] || continue
  case "$line" in -*) EXCL+=("${line#-}") ;; *) DEPLOY+=("$line") ;; esac
done < "$KIT/deploy_files.txt"
for f in "${DEPLOY[@]}"; do
  [ -f "$NEW/$f" ] || { echo "   MISSING in the commit: $f"; exit 1; }
  echo "$(sha256sum "$NEW/$f" | cut -d' ' -f1)  $f" >> "$OUT/DEPLOY_SET.txt"
done
sed 's/^/   /' "$OUT/DEPLOY_SET.txt"

echo "== reference (payload unit, 5 November)"
REF=""
if [ -n "$REF_TGZ" ] && [ -f "$REF_TGZ" ]; then
  mkdir -p "$TMP/ref" && tar xzf "$REF_TGZ" -C "$TMP/ref"
  REF="$TMP/ref/$(ls "$TMP/ref" | head -1)"
  echo "   $REF_TGZ"
else
  echo "   no REF_TGZ: the code comparison is skipped, the stored reference hashes are used"
fi

# radio-side files: the service's imports in either tree, the radio scripts and the native sources
{
  python3 "$KIT/tools/radio_closure.py" "$NEW"
  [ -n "$REF" ] && python3 "$KIT/tools/radio_closure.py" "$REF"
  (cd "$NEW" && find deploy/radio deploy/g40 deploy/docker native requirements-radio.txt -type f 2>/dev/null | grep -v '^native/lib/')
  [ -n "$REF" ] && (cd "$REF" && find deploy/radio deploy/g40 deploy/docker native -type f 2>/dev/null | grep -v '^native/lib/')
  printf '%s\n' "${DEPLOY[@]}" "$NATIVE_REL"
} | sort -u > "$OUT/RADIO_FILES.txt"

if [ -n "$REF" ]; then
  : > "$OUT/REFERENCE_PAYLOAD.sha256"
  while read -r f; do
    [ -f "$REF/$f" ] && echo "$(sha256sum "$REF/$f" | cut -d' ' -f1)  $f" >> "$OUT/REFERENCE_PAYLOAD.sha256"
  done < "$OUT/RADIO_FILES.txt"
  # the five radio files of the 5 November 09:09 deploy to the payload G40
  REFCHK=""
  for pair in 5ebc2715:dpmg/radio/dpmg_service.py 65b88320:dpmg/radio/sdr_core.py \
              17e09a3d:dpmg/radio/skiq_bridge.py fe59edaa:dpmg/radio/g40_clock.py \
              9b2439cd:dpmg/payload/rf_frontend.py; do
    want=${pair%%:*}; f=${pair#*:}
    have=$(sha256sum "$REF/$f" | cut -c1-8)
    [ "$have" = "$want" ] && REFCHK+="   $f $have matches the 5 November deploy record"$'\n' \
                          || REFCHK+="   $f $have DOES NOT match the deploy record ($want)"$'\n'
  done
else
  PREV="$KIT/REFERENCE_PAYLOAD.sha256"
  [ -f "$PREV" ] || { echo "   no reference hashes: $PREV is missing"; exit 1; }
  cp "$PREV" "$OUT/REFERENCE_PAYLOAD.sha256"
  REFCHK="   (reference hashes copied from $PREV)"$'\n'
fi

echo "== radio diff"
{
  echo "Radio-side files: payload G40 code of 5 November (reference) against commit $SHORT"
  echo "Built $(date -u '+%Y-%m-%d %H:%M UTC') by make_site_bundle.sh"
  echo
  echo "Reference check:"
  printf '%s' "$REFCHK"
  echo
  printf '%-44s %-9s %-9s %-15s %s\n' file ref commit status deploy
  while read -r f; do
    r=$(grep -F "  $f" "$OUT/REFERENCE_PAYLOAD.sha256" | awk -v f="$f" '$2==f{print substr($1,1,8)}')
    [ -n "$r" ] || r=none
    if [ -f "$NEW/$f" ]; then c=$(sha256sum "$NEW/$f" | cut -c1-8); else c=none; fi
    if [ "$r" = "$c" ]; then st=same
    elif [ "$r" = none ]; then st="new file"
    elif [ "$c" = none ]; then st="gone"
    elif [ -n "$REF" ] && [[ "$f" == *.py ]]; then
      st=$(python3 "$KIT/tools/ast_same.py" "$REF/$f" "$NEW/$f" 2>/dev/null || echo "CODE DIFFERS")
    elif [ -n "$REF" ] && [[ "$f" == *.sh ]]; then
      if diff -q <(grep -v '^[[:space:]]*#' "$REF/$f") <(grep -v '^[[:space:]]*#' "$NEW/$f") >/dev/null
      then st="comments only"; else st="CODE DIFFERS"; fi
    else st="differs"
    fi
    d=no
    for x in "${DEPLOY[@]}"; do [ "$x" = "$f" ] && d=yes; done
    for x in "${EXCL[@]}"; do [ "$x" = "$f" ] && d="left out"; done
    printf '%-44s %-9s %-9s %-15s %s\n' "$f" "$r" "$c" "$st" "$d"
  done < "$OUT/RADIO_FILES.txt"
} > "$OUT/RADIO_DIFF.txt"
awk 'NR>1 && $0 ~ /CODE DIFFERS|new file|gone|differs$|differs /' "$OUT/RADIO_DIFF.txt" | sed 's/^/   /'
WARN=$(awk '($4" "$5=="CODE DIFFERS" || $4=="differs" || $4" "$5=="new file") && $NF=="no"' "$OUT/RADIO_DIFF.txt")
if [ -n "$WARN" ]; then
  echo "   WARNING: these differ from the payload's code but are neither deployed nor listed as left out:"
  echo "$WARN" | sed 's/^/     /'
fi

( cd "$OUT" && { sha256sum "$NAME.tar.gz"; echo "$NSHA  $NATIVE_REL"; cat DEPLOY_SET.txt; } | awk '!seen[$2]++' > SHA256SUMS )
{
  echo "Site deploy bundle for the payload G40 (Payload 2)"
  echo "commit:        $FULL"
  git -C "$REPO" log -1 --format='subject:       %s%ncommit date:   %ci' "$FULL"
  echo "built:         $(date -u '+%Y-%m-%d %H:%M UTC') on $(hostname)"
  echo "archive:       $NAME.tar.gz"
  echo "archive bytes: $ASIZE"
  echo "archive sha256: $ASHA"
  echo "native drain:  $NATIVE_REL sha256 $NSHA (from $NATIVE_LIB, not in git)"
  echo "deploy set:    ${#DEPLOY[@]} files, DEPLOY_SET.txt"
  echo "reference:     $( [ -n "$REF" ] && echo "$REF_TGZ" || echo "stored hashes" )"
} > "$OUT/BUNDLE_INFO.txt"
echo "$SHORT" > "$KIT/bundle/CURRENT"
echo "== done: $OUT (bundle/CURRENT = $SHORT)"
