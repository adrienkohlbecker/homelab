local ret_status="%(?:%{$fg[green]%}➜ :%{$fg[red]%}➜ %s)%{$reset_color%}"
if [ $UID -eq 0 ]; then NCOLOR="red"; else NCOLOR="white"; fi

PROMPT='${ret_status} %{$fg[$NCOLOR]%}%n%{$fg[white]%}@%m %{$fg[cyan]%}%c %{$fg[blue]%}$(git_prompt_info)%{$fg[blue]%} % %{$reset_color%}'

ZSH_THEME_GIT_PROMPT_PREFIX="git:(%{$fg[red]%}"
ZSH_THEME_GIT_PROMPT_SUFFIX="%{$reset_color%}"
ZSH_THEME_GIT_PROMPT_DIRTY="%{$fg[blue]%}) %{$fg[yellow]%}✗%{$reset_color%}"
ZSH_THEME_GIT_PROMPT_CLEAN="%{$fg[blue]%})"
