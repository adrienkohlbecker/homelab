#!/usr/bin/env bash
set -euo pipefail

mkdir -p test/out
rm -f test/out/*.ansi

doit() {
  (
    test/testrole.sh $1 --checkmode 2> >(
      while read line; do
        if [[ "$line" == "+"* ]]; then
          echo -e "\e[2;30m$line\e[0m" >&2
        else
          echo -e "\e[0;41m$line\e[0m" >&2
        fi
      done
    )
  ) &> test/out/$1.ansi || (
    echo -e "\e[0;41m$1 failed\e[0m" >&2
    exit 1
  )
}
export -f doit

PARALLEL="parallel --jobs 5 --joblog test/out.log --eta doit"
if [[ ${1:-} == "--onlyfailed" ]]; then
  shift

  test/showfailed.sh | $PARALLEL
else
  find roles -mindepth 3 -maxdepth 3 -wholename "roles/*/tasks/main.yml" -print0 | xargs -0 -n1 dirname | xargs -n1 dirname | xargs -n1 basename | sort | $PARALLEL
fi
