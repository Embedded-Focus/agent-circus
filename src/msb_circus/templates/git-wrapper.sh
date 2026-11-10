#!/bin/bash
# msb-circus git wrapper (installed as /usr/local/bin/git by msb-circus;
# local edits are overwritten on every sandbox start).
#
# Keeps "git worktree add" and "git worktree move" targets inside the
# repository's .agent-circus/worktrees/ (or Claude Code's .claude/worktrees/)
# so new worktrees live on the host mount. Every other command runs the
# real git unchanged. This guides agents; it is not a security boundary.
real_git="${MSB_CIRCUS_REAL_GIT:-/usr/bin/git}"
# With [git] worktree_mirror, the workspace is mounted at guest_root and a
# second time at mirror_root (its host path). Only worktrees made through
# mirror_root record paths that are valid on the host. msb-circus fills in
# both values on install; mirror_root stays empty without the mirror.
guest_root=${MSB_CIRCUS_GUEST_ROOT-@GUEST_ROOT@}
mirror_root=${MSB_CIRCUS_MIRROR_ROOT-@MIRROR_ROOT@}

args=("$@")
n=${#args[@]}
globals=()
cwd=$PWD
i=0
# Skip git's global options; remember -C so relative paths resolve like git.
while [ "$i" -lt "$n" ]; do
    a=${args[$i]}
    case $a in
        -C)
            v=${args[$((i + 1))]-}
            if [ -n "$v" ]; then
                case $v in
                    /*) cwd=$v ;;
                    *) cwd=$cwd/$v ;;
                esac
            fi
            globals+=("$a" "$v")
            i=$((i + 2))
            ;;
        -c | --git-dir | --work-tree | --namespace | --config-env)
            globals+=("$a" "${args[$((i + 1))]-}")
            i=$((i + 2))
            ;;
        -*)
            globals+=("$a")
            i=$((i + 1))
            ;;
        *) break ;;
    esac
done

[ "${args[$i]-}" = worktree ] || exec "$real_git" "$@"
action=${args[$((i + 1))]-}
case $action in
    add) wanted=1 ;;
    move) wanted=2 ;;
    *) exec "$real_git" "$@" ;;
esac

# Find the target path: the wanted-th positional argument after the action.
# Remember its index (and the index of the source of a move) to build a
# corrected command for the error message.
target=
target_idx=
source_idx=
count=0
j=$((i + 2))
only_positionals=0
while [ "$j" -lt "$n" ]; do
    a=${args[$j]}
    if [ "$only_positionals" = 0 ]; then
        case $a in
            --) only_positionals=1; j=$((j + 1)); continue ;;
            -b | -B | --reason) j=$((j + 2)); continue ;;
            -*) j=$((j + 1)); continue ;;
        esac
    fi
    count=$((count + 1))
    if [ "$count" = "$wanted" ]; then
        target=$a
        target_idx=$j
        break
    fi
    source_idx=$j
    j=$((j + 1))
done
[ -n "$target" ] || exec "$real_git" "$@"

common=$("$real_git" "${globals[@]}" rev-parse --path-format=absolute \
    --git-common-dir 2>/dev/null) || exec "$real_git" "$@"
# Only guard non-bare repositories (common dir "<checkout>/.git").
[ "$(basename -- "$common")" = .git ] || exec "$real_git" "$@"
root=$(dirname -- "$common")
git_root=$(realpath -m -- "$root")

case $target in
    /*) path=$target ;;
    *) path=$cwd/$target ;;
esac
path=$(realpath -m -- "$path")

mirrored=0
if [ -n "$mirror_root" ] && [ -n "$guest_root" ] &&
    [ "$git_root" = "$(realpath -m -- "$guest_root")" ]; then
    # Git would record guest_root paths, which are invalid on the host.
    # Reject every target and point the agent to the host path instead.
    root=$mirror_root
    mirrored=1
else
    for dir in "$git_root/.agent-circus/worktrees" "$git_root/.claude/worktrees"; do
        case $path in
            "$dir"/?*) exec "$real_git" "$@" ;;
        esac
    done
fi

# Agents fix their command fastest when the error hands them one that
# works, so rebuild theirs. Worktree name: the part below a worktrees
# directory, else the new branch (-b/-B), else the last path component;
# a placeholder for paths that name no worktree (the repository or a
# worktrees directory itself).
name=
branch=
k=$((i + 2))
while [ "$k" -lt "$n" ]; do
    case ${args[$k]} in
        --) break ;;
        -b | -B) branch=${args[$((k + 1))]-} ;;
    esac
    k=$((k + 1))
done
for dir in "$git_root/.agent-circus/worktrees" "$git_root/.claude/worktrees"; do
    case $path in
        "$dir"/?*) name=${path#"$dir"/} ;;
    esac
done
if [ -z "$name" ]; then
    case $path in
        "$git_root" | "$git_root"/.agent-circus | "$git_root"/.agent-circus/worktrees | \
            "$git_root"/.claude | "$git_root"/.claude/worktrees | /) ;;
        *) name=${branch:-$(basename -- "$path")} ;;
    esac
fi

# Run the command in the repository root (the host path with the mirror)
# and replace the target. A relative move source becomes absolute, mapped
# from guest_root to mirror_root.
suggest=git
k=0
while [ "$k" -lt "${#globals[@]}" ]; do
    if [ "${globals[$k]}" = -C ]; then
        k=$((k + 2))
        continue
    fi
    suggest+=" $(printf '%q' "${globals[$k]}")"
    k=$((k + 1))
done
suggest+=" -C $(printf '%q' "$root")"
k=$i
while [ "$k" -lt "$n" ]; do
    a=${args[$k]}
    if [ "$k" = "$target_idx" ]; then
        if [ -n "$name" ]; then
            suggest+=" $(printf '%q' ".agent-circus/worktrees/$name")"
        else
            suggest+=" .agent-circus/worktrees/<name>"
        fi
    else
        if [ "$k" = "$source_idx" ]; then
            case $a in
                /*) ;;
                *) a=$(realpath -m -- "$cwd/$a") ;;
            esac
            if [ "$mirrored" = 1 ]; then
                g=$(realpath -m -- "$guest_root")
                case $a in
                    "$g") a=$mirror_root ;;
                    "$g"/*) a=$mirror_root/${a#"$g"/} ;;
                esac
            fi
        fi
        suggest+=" $(printf '%q' "$a")"
    fi
    k=$((k + 1))
done

{
    echo "fatal: msb-circus: rejected worktree path $path"
    echo "Worktrees must live below $root/.agent-circus/worktrees/ so they stay visible on the host."
    if [ "$mirrored" = 1 ]; then
        echo "$guest_root and $mirror_root are the same checkout, mounted twice in this sandbox."
        echo "Run worktree commands through $mirror_root so Git records paths that exist on the host."
    fi
    echo "Run this instead:"
    echo "  $suggest"
} >&2
exit 128
