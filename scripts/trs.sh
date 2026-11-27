#!/usr/bin/env bash
set -euo pipefail

script_name="$(basename -- "$0")"
if (( $# < 1 )); then
    echo "Usage: ${script_name} raw|compile|test|<ll_CC>" >&2
    exit 1
fi

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd -- "${script_dir}/.."

tr_dir="plugin/src/HelysIum/I18n/trs"
pot_file="${tr_dir}/raw.pot"
mkdir -p -- "$tr_dir"

gen() {
    local lang="$1"
    shift
    if (( $# == 0 )); then
        return 0
    fi
    xgettext -L "$lang" \
        -o "$pot_file" \
        --join-existing \
        --from-code=UTF-8 \
        --no-wrap \
        --add-location=file \
        --add-comments=TRANSLATORS: \
        --package-name=helysium-shell \
        -k \
        -ktr:1 -ktrCtx:2c,1 -ktrMarked:1 \
        -ktrN:1,2 -ktrCtxN:4c,1,2 \
        -kmark:1 -kmarkCtx:2c,1 \
        -kmarkN:1,2 -kmarkCtxN:4c,1,2 \
        "$@"
}

compile() {
    local po="$1"
    local mo="$2"
    msgfmt --check-format -o "$mo" "$po"
}

gen_trs() {
    : > "$pot_file"

    local -a qml_files=()
    local -a cpp_files=()
    while IFS= read -r -d '' f; do
        qml_files+=("${f#./}")
    done < <(find . -path './build' -prune -o -name '*.qml' -type f -print0 | sort -z)

    while IFS= read -r -d '' f; do
        cpp_files+=("${f#./}")
    done < <(find . -path './build' -prune -o \( -name '*.cpp' -o -name '*.hpp' \) -type f -print0 | sort -z)

    gen JavaScript "${qml_files[@]}"
    gen C++ "${cpp_files[@]}"

    # Strip js format then tag qt-format, with qt-plural-format for the %n count
    perl -00 -i -pe '
        s/^#, javascript-format\n//m;
        s/, javascript-format//;

        my @fmts;
        push @fmts, "qt-format" if /%L?[1-9]/;
        push @fmts, "qt-plural-format" if /%L?n/;

        if (@fmts && !/^#,.*qt-format/m) {
            my $fmts = join ", ", @fmts;
            s/^#,/#, $fmts,/m or s/^(?=msgctxt |msgid )/#, $fmts\n/m
        }
    ' "$pot_file"
}

case "$1" in
    raw)
        gen_trs
        exit 0
        ;;
    compile)
        if (( $# < 3 )); then
            echo "Usage: ${script_name} compile <ll_CC> <out_file>" >&2
            exit 1
        fi
        compile "${tr_dir}/${2}.po" "$3"
        exit 0
        ;;
esac

[[ -f "$pot_file" ]] || gen_trs

if [[ "$1" == "test" ]]; then
    if (( $# < 2 )); then
        echo "Usage: ${script_name} test <out_file>" >&2
        exit 1
    fi

    tmp_dir="$(mktemp -d)"
    trap 'rm -rf -- "$tmp_dir"' EXIT
    test_po="${tmp_dir}/test.po"

    msginit --locale=en_US \
        --input="$pot_file" \
        --output="$test_po" \
        --no-translator 2>/dev/null
    sed -Ei 's/^msgstr(\[[0-9]+\])? "(.*)"$/msgstr\1 "[[ \2 ]]"/' "$test_po"
    compile "$test_po" "$2"
    exit 0
fi

msginit --locale="$1" \
    --input="$pot_file" \
    --output="${tr_dir}/${1}.po"
