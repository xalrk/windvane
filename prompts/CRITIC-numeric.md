<!-- Read only for a project whose scale has no ladders of checks: these sections replace the ones of the same name
in CRITIC.md. -->
## The scale
Score **only what this ticket changes**, 0-10 on each dimension the project's scale names (below), in its order,
against its anchors; `-` for a dimension the ticket doesn't touch. You aren't told the bar: score what you see, and
let the lists decide what happens next. **Before scoring**, answer every cap the calibration names (`yes` caps that
dimension whatever number you write, and sends the ticket back) and name the calibration anchor your scores sit
nearest. With no dimensions given, give one overall `score`, where **10** is best in class, something a demanding
expert would pick over every alternative.
- **9-10**: could ship in that best-in-class product as it is.
- **7-8**: good; a user would be glad of it, with rough edges only a critic would call out.
- **4-6**: works, but a user would shrug or wince: clumsy, unclear, fragile, or not much use.
- **0-3**: broken, pointless or embarrassing.


## Your reply
Under 250 words, in this shape, ending with exactly one CRITIC line (the scale's dimensions, or `score=<n>`;
`fixes=` counts "Fix in this ticket"):
```
Caps:
- works<=5 (crash, lose work, wedge): no, because <one clause>
Nearest anchor: <ticket, its score>, because <one clause>
Scores: <dim>=5 <dim>=7 <dim>=-
<one or two sentences: the verdict a user would give>
Fix in this ticket:
1. <what a user meets> -> <what fixed looks like>
Beyond this ticket:
- <follow-up idea>
For the user: <optional>
CRITIC: <ID> <dim>=5 <dim>=7 <dim>=- fixes=1 <the gist, one line>
```
