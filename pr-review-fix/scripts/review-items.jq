  def login: ascii_downcase | sub("\\[bot\\]$"; "");
  def hex: ascii_downcase | explode | reduce .[] as $c (0; . * 16 + (if $c >= 97 then $c - 87 else $c - 48 end));
  # URI bytes -> Unicode code points (encodeURIComponent escapes UTF-8).
  def utf8:
    if length == 0 then [] else
      .[0] as $b |
      (if $b < 128 then 1 elif $b < 224 then 2 elif $b < 240 then 3 else 4 end) as $n |
      (if $n == 1 then $b else
        (.[0] % (if $n == 2 then 32 elif $n == 3 then 16 else 8 end)) as $start |
        reduce .[1:$n][] as $c ($start; . * 64 + ($c % 64)) end) as $cp |
      [$cp] + (.[$n:] | utf8) end;
  def decode:
    # Decode escaped runs separately, preserving unescaped Unicode.
    [match("(?:%[0-9A-Fa-f]{2})+|[^%]+|%"; "g").string |
      if startswith("%") and length > 1 then
        [scan("[0-9A-Fa-f]{2}") | hex] | utf8 | implode
      else . end] | join("");
  def reason: split("\n") | map(select(test("^\\s*>") | not)) | join("\n") | gsub("^\\s+|\\s+$"; "");
  def reason_length: reason | explode | map(if . > 65535 then 2 else 1 end) | add // 0;
  def text:
    gsub("\\n*```suggestion\\n[\\s\\S]*?\\n```"; "") |
    gsub("<!--[\\s\\S]*?-->"; "") |
    sub("^\\s*(?:⚖️\\s*)?\\*\\*MergeJudge \\((?:LLM|AI)\\):\\*\\*\\s*"; "") |
    gsub("\\\\(?<char>[\\\\`*_{}\\[\\]()#+.!|<>~-])"; .char) |
    gsub("^\\s+|\\s+$"; "");
  ($actions | split(",") | map(login)) as $actionNames |
  def source($who; $type; $body):
    ($who | login) as $w |
    if ($body | test("<!-- mergejudge:(inline|summary)")) and ($actionNames | index($w)) != null then "mergejudge-action"
    elif $w == "mergejudge" then "mergejudge-app"
    elif $w == "copilot-pull-request-reviewer" or $w == "copilot" then "copilot"
    elif $w == "coderabbitai" then "coderabbit"
    elif $w == "cursor" then "bugbot"
    elif $type == "Bot" then "other-bot" else "human" end;
  def summary($source):
    if $source == "mergejudge-action" or $source == "mergejudge-app" then test("<!-- mergejudge:summary(?::action)? -->")
    elif $source == "coderabbit" then test("(?i)(<!--.*(walkthrough|summary|summarize)|^\\s*#{1,3}\\s*(walkthrough|summary))")
    elif $source == "copilot" then test("(?i)^\\s*#{1,3}\\s*(pull request overview|overview|pull request summary)")
    else false end;
  def preview: text | .[0:500];
  def reference($item):
    . as $body |
    (($item.html_url // "") as $url | $url != "" and ($body | contains($url))) or
    ($body | test("(^|[^A-Za-z0-9_])" + ($item.id | tostring) + "([^A-Za-z0-9_]|$)"));
  ($threads[0][0].data.repository.pullRequest.author.login // "") as $prAuthor |
  [
    ($threads[0][] | .data.repository.pullRequest.reviewThreads.nodes[] |
      . as $t | .comments.nodes[0] as $root |
      ($root.body // "") as $body |
      ($body | [capture("<!-- mergejudge:inline key=(?<key>\\S+) state=(?<state>active|resolved)(?: [a-z0-9]+=\\S*)* -->")][0] // {}) as $m |
      source($root.author.login; $root.author.__typename; $body) as $source |
      ($source == "mergejudge-action" or $source == "mergejudge-app" or ($m.key != null)) as $mj |
      [.comments.nodes[1:][] | {id:(.databaseId // .id), author:.author.login,
        authorType:.author.__typename, isBot:(.author.__typename == "Bot"), body:(.body // ""), createdAt:.createdAt}] as $replies |
      {kind:"thread", id:$t.id, threadId:$t.id, rootCommentId:($root.databaseId // $root.id),
       url:($root.url // ("https://github.com/"+$repo+"/pull/"+$pr+"#discussion_r"+($root.databaseId|tostring))),
       author:$root.author.login, authorType:($root.author.__typename // "User"), source:$source,
       path:$t.path, line:$t.line, key:(if $m.key then $m.key | decode else "\($t.path):\($t.line)" end),
       mjState:($m.state // null), isResolved:$t.isResolved,
       countsForGate:($source == "mergejudge-action" and $m.key != null),
       answered:($t.isResolved or $m.state == "resolved" or any($replies[];
         # An updated MergeJudge root may have been re-flagged after old replies.
         ((($mj | not) or $root.updatedAt == null or .createdAt == null or .createdAt >= $root.updatedAt) and
          (((.isBot | not) and (.body | reason_length) >= 8) or
           (($mj | not) and ((.author | ascii_downcase) == ($me | ascii_downcase) or
             ($prAuthor != "" and (.author | ascii_downcase) == ($prAuthor | ascii_downcase)))))))),
       message:($body | preview), replies:$replies} |
       .needsAnswer = (.answered | not)),
    ((($reviews[0][] | select(.state == "CHANGES_REQUESTED" or .state == "COMMENTED") | . + {_kind:"review"}),
       ($comments[0][] | . + {_kind:"conversation"})) |
      . as $item | (.body // "") as $body |
      select(($body | test("\\S")) and ((.user.login | ascii_downcase) != ($me | ascii_downcase))) |
      source(.user.login; .user.type; $body) as $source |
      ($body | summary($source)) as $summary |
      [$comments[0][] | select((.user.login | ascii_downcase) == ($me | ascii_downcase)) |
        select((.created_at // "") > ($item.submitted_at // $item.created_at // "")) |
        select(.body | reference($item)) |
        {id,author:.user.login,authorType:.user.type,body,url:.html_url}] as $replies |
      {kind:(if $summary then "summary" else ._kind end), id:.id, threadId:null, rootCommentId:.id,
       url:.html_url, author:.user.login, authorType:(.user.type // "User"), source:$source,
       path:null,line:null,key:((if ._kind == "review" then "review:" else "comment:" end)+(.id|tostring)),
       mjState:null,isResolved:false,countsForGate:false,answered:($replies|length > 0),
       needsAnswer:(($summary | not) and ($replies | length == 0)), message:($body | preview),replies:$replies})
  ] | map(select(($open | not) or .needsAnswer))
