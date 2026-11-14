#!/bin/bash
# msb-circus git wrapper (installed as /usr/local/bin/git by msb-circus;
# local edits are overwritten on every sandbox start).
#
# Keeps "git worktree add" and "git worktree move" targets inside the
# repository's .agent-circus/worktrees/ (or Claude Code's .claude/worktrees/)
# so new worktrees live on the host mount. With the worktree mirror, it
# also runs worktree commands that go through the guest mount through the
# host path instead, so Git records host paths. Every other command runs
# the real git unchanged. This guides agents; it is not a security boundary.
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
# Set when --git-dir or --work-tree name the repository explicitly.
dir_globals=0
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
        --git-dir | --work-tree)
            dir_globals=1
            globals+=("$a" "${args[$((i + 1))]-}")
            i=$((i + 2))
            ;;
        --git-dir=* | --work-tree=*)
            dir_globals=1
            globals+=("$a")
            i=$((i + 1))
            ;;
        -c | --namespace | --config-env)
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
# wanted: position of the path that must stay in a worktrees directory
# (add: the new worktree, move: the destination); 0 for actions that are
# only translated to the mirror root.
case $action in
    add) wanted=1 ;;
    move) wanted=2 ;;
    remove | lock | unlock | repair) wanted=0 ;;
    *) exec "$real_git" "$@" ;;
esac

mirror=0
[ -n "$mirror_root" ] && [ -n "$guest_root" ] && mirror=1
[ "$wanted" -gt 0 ] || [ "$mirror" = 1 ] || exec "$real_git" "$@"

# Collect the indexes of the positional arguments after the action.
pos=()
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
    pos+=("$j")
    j=$((j + 1))
done

# Path arguments per action: add <path> [<commit-ish>], move <src> <dst>,
# remove/lock/unlock <path>, repair [<path>...]. Remember the target and
# the source of a move to build a corrected command for the error message.
target=
target_idx=
source_idx=
path_idx=()
case $action in
    add) path_idx=("${pos[@]:0:1}") ;;
    move) path_idx=("${pos[@]:0:2}") ;;
    remove | lock | unlock) path_idx=("${pos[@]:0:1}") ;;
    repair) path_idx=("${pos[@]}") ;;
esac
if [ "$wanted" -gt 0 ]; then
    target_idx=${pos[$((wanted - 1))]-}
    [ -n "$target_idx" ] || exec "$real_git" "$@"
    target=${args[$target_idx]}
    [ "$wanted" = 2 ] && source_idx=${pos[0]}
fi

common=$("$real_git" "${globals[@]}" rev-parse --path-format=absolute \
    --git-common-dir 2>/dev/null) || exec "$real_git" "$@"
# Only guard non-bare repositories (common dir "<checkout>/.git").
[ "$(basename -- "$common")" = .git ] || exec "$real_git" "$@"
root=$(dirname -- "$common")
git_root=$(realpath -m -- "$root")

# Print the absolute, normalized form of a path argument.
abs_path() {
    case $1 in
        /*) realpath -m -- "$1" ;;
        *) realpath -m -- "$cwd/$1" ;;
    esac
}

# With the mirror, guest_root and mirror_root are the same directory, but
# Git records the path it was given. Translate worktree commands of this
# checkout that go through guest_root to mirror_root, so Git records host
# paths: run them in mirror_root with every path argument mapped. Skipped
# with --git-dir/--work-tree, whose values would need mapping too.
translate=0
declare -A mapped=()
if [ "$mirror" = 1 ] && [ "$dir_globals" = 0 ]; then
    g=$(realpath -m -- "$guest_root")
    m=$(realpath -m -- "$mirror_root")
    if [ "$git_root" = "$g" ] || [ "$git_root" = "$m" ]; then
        [ "$git_root" = "$g" ] && translate=1
        for idx in "${path_idx[@]}"; do
            p=$(abs_path "${args[$idx]}")
            case $p in
                "$g") p=$mirror_root; translate=1 ;;
                "$g"/*) p=$mirror_root/${p#"$g"/}; translate=1 ;;
            esac
            mapped[$idx]=$p
        done
    fi
fi

# Run the translated command: the global options without -C (all paths
# are absolute now), then -C mirror_root and the mapped arguments.
run_translated() {
    local argv=() k=0
    while [ "$k" -lt "${#globals[@]}" ]; do
        if [ "${globals[$k]}" = -C ]; then
            k=$((k + 2))
            continue
        fi
        argv+=("${globals[$k]}")
        k=$((k + 1))
    done
    argv+=(-C "$mirror_root")
    k=$i
    while [ "$k" -lt "$n" ]; do
        if [ -n "${mapped[$k]+set}" ]; then
            argv+=("${mapped[$k]}")
        else
            argv+=("${args[$k]}")
        fi
        k=$((k + 1))
    done
    echo "msb-circus: running \"git worktree $action\" through $mirror_root," \
        "the host path of $guest_root (the same directory), so Git records" \
        "paths that are valid on the host." >&2
    exec "$real_git" "${argv[@]}"
}

if [ "$wanted" = 0 ]; then
    [ "$translate" = 1 ] && run_translated
    exec "$real_git" "$@"
fi

mirrored=0
if [ "$translate" = 1 ]; then
    path=${mapped[$target_idx]}
    git_root=$m
    root=$mirror_root
    mirrored=1
    for dir in "$mirror_root/.agent-circus/worktrees" "$mirror_root/.claude/worktrees"; do
        case $path in
            "$dir"/?*) run_translated ;;
        esac
    done
    # Compare below the normalized mirror root from here on.
    case $path in
        "$mirror_root") path=$m ;;
        "$mirror_root"/*) path=$m/${path#"$mirror_root"/} ;;
    esac
else
    path=$(abs_path "$target")
    if [ "$mirror" = 1 ] && [ "$git_root" = "$(realpath -m -- "$guest_root")" ]; then
        # Only reached with --git-dir/--work-tree: Git would record
        # guest_root paths, which are invalid on the host. Reject every
        # target and point the agent to the host path instead.
        root=$mirror_root
        mirrored=1
    else
        for dir in "$git_root/.agent-circus/worktrees" "$git_root/.claude/worktrees"; do
            case $path in
                "$dir"/?*) exec "$real_git" "$@" ;;
            esac
        done
    fi
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

# Explain why. Agents act on the first lines, so the command comes first,
# then one reason that matches this rejection, then the warning against
# bypassing the wrapper (the most common wrong reaction).
if [ "$mirrored" = 1 ] && [ "$translate" = 0 ]; then
    why="$guest_root and $mirror_root are the same checkout, mounted twice in this sandbox. With --git-dir or --work-tree, Git would record $guest_root paths, which do not exist on the host."
else
    case $path in
        "$git_root" | "$git_root"/*)
            why="A worktree needs its own directory below $root/.agent-circus/worktrees/ (or .claude/worktrees/). That directory is part of the workspace, so the worktree and its files stay visible on the host."
            ;;
        *)
            why="$path is outside the workspace. A worktree there would exist only in this sandbox and would be lost when the sandbox is removed."
            ;;
    esac
fi
{
    echo "fatal: msb-circus: rejected worktree path $path"
    echo "Run this instead:"
    echo "  $suggest"
    echo "Why: $why"
    echo "Do not use /usr/bin/git to get around this check: the worktree would not work correctly on the host."
} >&2
exit 128
