"""The words a text may be made of to pass the redaction pass.

The pass (`beacon.redact.screen`) lets a text through only when every word of it is
one of these or part of a name somebody listed. So this is a list of the words a
notice to the public and a plain account of an incident are written in: what happened,
where, when, who responded, and what an authority said. It leaves out, on purpose:

- **Words about a person's body or fate** (injured, burns, hospital, patient, died),
  so a text that describes somebody's condition does not pass.
- **Words about who a person is** (man, woman, child, age, old, and titles such as
  Mr), so a text that describes a private person does not pass.
- **Words that are mostly names.** A word that is both an ordinary word and a common
  name (`may`, `will`) is here only when notices cannot be written without it, and
  the pass refuses it where it is written with a capital in the middle of a sentence.

There are four lists:

- `WORDS`: what a text in Beacon's own voice may be made of. None of them tells a
  reader what to do.
- `ORDER_WORDS`: what only an authority's notice may use besides: the words an
  instruction is made of (`leave`, `stay`, `must`, `you`), and the words that say
  an order holds or is over (`effect`, `force`, `lifted`). A text a model wrote for
  Beacon that holds one of them does not pass, which is how "Beacon never says stay
  or go" is kept for what a model writes, and how a model is kept from saying that
  an order is in force.
- `PROPER`: words that may be written with a capital anywhere.
- `PUBLIC_NAMES`: public bodies, which a text may name. A public place a notice
  sends people to is on a list an operator gives (`Names`).

A text that uses a word that is not here is left out whole, never cut, and whoever
shows it says so and links the original. That is the safe failure, and it is common:
the fix for a word that plainly belongs is to add it here, with a second look at
whether it can describe a person or tell one what to do. Never add a way past the
lists.
"""

_WORDS = """
a about above across active activity added additional advisory affected after again
against ahead air aircraft alarm alarms alert alerts all along already also an and
another any are area areas around arrive arrived arriving as asked at authorities
authority available away

back barricade barricades battalion be became because been before began begin begins
behind being below beside between block blocked blocks both bridge brief building
buildings but by

call called calls came can cancelled cannot cause caused center checked circled
circling city civic clear cleared clearing close closed closes closure closures
collision come coming command confirmed contained containment continue continues
continuing control corner could county crew crews cross crossing current currently

danger dangerous day days declared department departments detour detours direction
dispatch dispatched dispatcher district division do document documents does doors
down downtown drawn dropped due during

each early earlier earthquake east eastbound effect either electrical emergency end ended
ends engine engines enter entered entrance entry equipment evacuate evacuated
evacuation evacuations evening event events every everyone except exit expected
expects expires exposure extended extinguished

far feed few fire firefighters fires first five flames flood flooding floor floors
followed following foot for force four from front full further

gas gave given gives go goes going gone ground group

had half has have hazard hazardous hazmat heard heavy held helicopter helicopters here
high highway hold holds hour hours how however

if immediate immediately in incident incidents include includes including indoors
information inside inspected inspection instruction instructions intersection into
investigation is issued issues it its

just

keep kept knocked known

lane lanes large larger last late later least leave leaves leaving left less level
lifted lifts light line lines listed lists local location long longer low lower

made main major management many map material materials may means medic medical
mentioned mention mentions message messages midnight mile miles minor minute minutes
moment more morning most moved moving much multiple must

national near nearby nearest need needed neighborhood neighboring neighbouring new
next night no none noon north northbound not nothing notice notices now number

of off office officers official officially officials on once one only open opened
opens operations or order ordered orders other others out outage outside over overhaul
overhead own

part partial parts pass passed past people pipe place places plaza point police
portion possible posted power present press pressed progress public published

radio rail ramp reach reached read reading readings ready reason received reception
recorded reduced reference release released remain remains reopen reopened reopening
replaced report reported reports request requested rescue resident residents respond
responded responders responding response rest restricted result return returned road
roads route routes

safe safety said same say says scene school second section see seen sent service
services set several shelter sheltering shelters short show showing shown shows shut
side sides sidewalk sign signal signals since site six small smaller smoke so some
soon source sources south southbound spread spreading staged staging start started
starts state stated statement station stay stays still stop stopped stops street
streets structure structures such summary system

taken tap taps team teams ten than that the their them then there these they third
this those three through time times to today together told tonight toward towards
traffic transit truck trucks turn turned two

unconfirmed under underground unit units unknown unless until up update updated
updates upper use used using utilities utility

vegetation vehicle vehicles ventilation very via visible

wait waiting warning warnings was watch water way we weather week were west westbound
what when where whether which while who whole wide widened wider will wind windows
winds wire wires with within without work working would

yard yet zone zones

am pm utc pst pdt

zero eight nine eleven twelve twenty thirty forty fifty hundred thousand
first second third fourth fifth

invented scenario mock
"""

# The words an instruction is made of. Only an authority's notice may use them.
_ORDER_WORDS = """
advised avoid bring call can't come do don't enter evacuate follow go keep leave
may must need needs please remain return shall shelter should stay stop take turn
urged use wait you your yours yourself

effect force lifted lifts
"""
ORDER_WORDS: frozenset[str] = frozenset(_ORDER_WORDS.split())

# Never among Beacon's own words, whatever the list above them says.
WORDS: frozenset[str] = frozenset(_WORDS.split()) - ORDER_WORDS

# Written with a capital wherever they stand.
_PROPER = """
beacon ember nws sffd sfpd sfmta dem caltrans chp pge muni bart ems utc pst pdt
monday tuesday wednesday thursday friday saturday sunday
january february march april june july august september october november december
"""
PROPER: frozenset[str] = frozenset(_PROPER.split())

# Public bodies a text may name, as they are written.
PUBLIC_NAMES: tuple[str, ...] = (
    "San Francisco",
    "City and County of San Francisco",
    "San Francisco Fire Department",
    "Fire Department",
    "San Francisco Police Department",
    "Police Department",
    "San Francisco Department of Emergency Management",
    "Department of Emergency Management",
    "San Francisco Department of Public Health",
    "San Francisco Public Works",
    "San Francisco Municipal Transportation Agency",
    "San Francisco Public Utilities Commission",
    "National Weather Service",
    "California Highway Patrol",
    "Pacific Gas and Electric",
    "Muse Ember",
)

# Phrases that hold a word of `ORDER_WORDS` and are the name of a thing, not an
# instruction: a shelter-in-place order is called that in Beacon's own voice too.
FIXED_PHRASES: tuple[str, ...] = ("shelter in place",)
