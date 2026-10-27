# Token Bucket Rate Limiter - LEARN.md

Ye file tumhare liye hai. Is project ko line by line samjhaya gaya hai,
bilkul shuru se, aise jaise koi teacher paas baitha ke samjha raha ho.
Pehle concept, phir math, phir code. Code chalane se pehle ye padh lo,
phir har cheez khud se clear hoti jayegi.

---

## Part 1: Rate Limiting hota kya hai, aur companies ko iski zaroorat kyun padti hai?

Socho tumne ek API banayi hai - maan lo ek server jo logo ko data deta hai.
Tum chahte ho ki ek normal user ek second me, say, 10 requests bhej sake.
Lekin agar koi ek hi user ek second me 10,000 requests bhej de to kya hoga?

- Tumhara server un saari requests ko process karne me busy ho jayega.
- Baaki genuine users ko slow response milega ya error milega.
- Agar tum cloud pe ho, to har request = paisa. Bill badh jayega.
- Koi attacker jaan-boojh ke aisa kare to isko **DoS attack** (Denial of
  Service) kehte hain - server ko itna busy kar do ki asli users ke liye
  band hi ho jaye.

Isiliye har serious API ek **limit** rakhti hai: "ek client, ek time window
me, itni hi requests." Isi limit ko enforce karne ko **rate limiting**
kehte hain. Tumne khud ye dekha hoga:

- GitHub API: "API rate limit exceeded" wala message.
- ChatGPT / Groq: "Too many requests, please wait" - HTTP status **429**.
- OTP systems: "5 galat try ke baad 30 minute rukho."

To ye koi textbook-only topic nahi hai - ye har production backend ka
roz ka kaam hai. Interview me bhi ye favourite topic hai, kyunki isse
pata chalta hai ki candidate ko sirf CRUD banana aata hai ya system ko
**protect** karna bhi aata hai.

**Is project me hum kya bana rahe hain?** Ek chhoti si FastAPI service jo:

1. Har client (API key ya IP address) ko alag se count karti hai.
2. Token bucket algorithm se decide karti hai: request allow ya reject.
3. Reject ho to HTTP 429 + "kitna wait karo" (Retry-After) bata deti hai.
4. Client ko headers me bata deti hai: tumhari limit kya hai, kitna bacha hai.

---

## Part 2: Teen mashhoor algorithms - Fixed Window, Leaky Bucket, Token Bucket

Rate limiting karne ke 3 classic tareeke hain. Teeno ka basic idea samjho,
interview me comparison pakka pucha jata hai.

### 2.1 Fixed Window Counter

Sabse simple. Ek counter rakho: "is minute me is user ne kitni requests
ki?" Limit 100/minute hai, counter 100 hua to reject. Minute khatam,
counter zero se restart.

**Problem (boundary burst):** User 12:00:59 pe 100 requests bhejta hai
(window khatam hone se 1 second pehle), phir 12:01:01 pe aur 100.
Do second ke andar 200 requests pass ho gayi, jabki limit "100/minute"
thi. Window ki boundary pe ye double burst wala loophole hai.

### 2.2 Leaky Bucket

Requests ek queue (bucket) me padti hain aur ek **fixed rate** se "leak"
hoti hain - matlab process hoti hain. Queue full hai to nayi request reject.
Ye traffic ko bilkul smooth kar deta hai, hamesha ek constant rate.

**Problem:** Genuine burst bhi allow nahi hota. Agar user ne ek saath
10 sahi requests bheji aur processing rate 1/second hai, to 10th request
ko 9 second wait karna padega - jabki server khaali baitha tha. Thoda
zyada strict hai, aur purani queued requests nayi se zyada important
ho jaati hain.

### 2.3 Token Bucket (hamara algorithm)

Ek bucket hai jisme **tokens** padte hain.

- Bucket ki max size = `capacity` (maan lo 10 tokens).
- Har second bucket me `refill_rate` tokens apne aap add hote hain
  (maan lo 1 token/second), lekin capacity se zyada kabhi nahi.
- Request aati hai to bucket se 1 token nikalta hai. Token hai to request
  allowed, bucket khaali hai to request rejected.

**Iska fayda dono duniya ka best hai:**

- Bucket full pada hai to ek saath **burst** of 10 requests allow hai
  (fixed window jaisi flexibility).
- Lambi race me average rate 1/second se zyada nahi ja sakta, kyunki
  tokens usi speed pe bante hain (leaky bucket jaisi protection).
- Koi queue nahi, koi wait nahi - decision instant hai: token hai ya nahi.

Isi liye Stripe, Amazon API Gateway, aur bahut se systems token bucket
(ya iske variants) use karte hain.

| Algorithm | Burst allowed? | Boundary problem? | Implementation |
|-----------|---------------|-------------------|----------------|
| Fixed Window | Haan, window ke andar | Haan, double burst | Sabse easy |
| Leaky Bucket | Nahi, fixed rate pe process | Nahi | Queue chahiye |
| Token Bucket | Haan, capacity tak | Nahi | Medium, maths simple |

---

## Part 3: Refill wali maths, numbers ke saath

Token bucket ka pura khel ek hi formula hai:

```
tokens_now = min(capacity, tokens_last_time + elapsed_seconds x refill_rate)
```

**Lazy refill trick:** Hum asal me koi background thread nahi chalate jo
har second token add kare. Hum bas yaad rakhte hain "last time bucket kab
check hua tha". Jab koi request aati hai, hum clock dekhte hain, elapsed
time nikalte hain, aur upar wala formula laga ke tokens update kar dete
hain. Result bilkul waisa hi hai jaise tokens continuously drip ho rahe
hon - lekin bina kisi extra thread ke. Isko **lazy refill** kehte hain.
Time ke liye `time.monotonic()` use hota hai - monotonic clock kabhi
peechhe nahi jaata (system clock change ho jaye to bhi), isiliye elapsed
time hamesha sahi nikalta hai.

**Ek poora example chala ke dekhte hain.** capacity = 5, refill_rate = 2
tokens/second.

1. t = 0.0 - Bucket full hai: 5 tokens. 5 requests aayi, sab allowed.
   Bache: 0 tokens.
2. t = 0.0 - Ek aur request. Tokens 0 hain. **Reject.**
   Kitna wait? Humein 1 token chahiye, rate 2/second hai, to
   wait = deficit / rate = 1 / 2 = **0.5 second**. Yahi `retry_after`
   hai jo hum client ko dete hain.
3. t = 0.5 - 0.5 second guzra, 0.5 x 2 = 1 token refill hua. Request
   aayi, allowed. Bache: 0.
4. t = 3.5 - 3 second idle. Refill = 3 x 2 = 6 tokens... lekin capacity
   5 hai, to bucket me sirf **5** honge. Ye `min(capacity, ...)` wala
   hissa hai - bucket overflow kabhi nahi hota, chahe user ek saal
   gayab rahe.

**Cost wala concept:** Har request ki price 1 token hona zaroori nahi.
Ek heavy operation (maan lo bada report generate karna) ki cost 5 tokens
bhi ho sakti hai. Formula waisa hi: tokens >= cost hai to allowed aur
`cost` tokens katega, warna wait = (cost - tokens) / rate.

Aur ek khaas case: agar `cost > capacity` ho (jaise bucket 5 ki hai aur
request 8 tokens maang rahi hai) to wo request **kabhi** allowed nahi ho
sakti - bucket me 8 tokens aa hi nahi sakte. Isiliye is case me hum
`retry_after = infinity` dete hain, matlab "rukne se bhi fayda nahi".

---

## Part 4: Code - file by file, function by function

Project ki structure bahut chhoti rakhi hai, jaan-boojh ke:

```
rate_limiter.py   -> sirf algorithm. Koi FastAPI nahi, koi HTTP nahi.
main.py           -> FastAPI app: middleware, endpoints, configuration.
tests/test_rate_limiter.py -> saari checking, automated.
```

### 4.1 `rate_limiter.py` - dil (core logic)

#### `RateLimitResult` (dataclass)

`allow()` function ka result ek simple object me pack karke dete hain:

- `allowed` (bool) - request ja sakti hai ya nahi.
- `limit` - bucket ki capacity.
- `remaining` - is check ke baad bache tokens.
- `retry_after_seconds` - allowed hai to 0; denied hai to kitne seconds
  rukna padega (exact calculation); cost > capacity ho to `inf`.
- `reset_after_seconds` - bucket ko phir se pura full hone me kitna time.

#### `BucketStatus` (dataclass)

Bucket ki photo (snapshot) bina token kharch kiye: capacity, refill rate,
current tokens. `full_after_seconds` ek property hai jo calculate karti
hai: `(capacity - tokens) / refill_rate`.

#### `TokenBucket` class

Ek bucket = ek client ka hisaab. Teen cheezein yaad rakhta hai:
`_tokens` (abhi kitne tokens hain), `_last_refill` (aakhri baar kab
refill calculate hua), aur ek `_lock`.

- `__init__(capacity, refill_rate, clock)` - Validation pehle: capacity
  ya rate zero/negative ho to `ValueError`. Bucket **full** shuru hota
  hai (naya user pehli baar me hi burst kar sakta hai - ye standard
  behaviour hai). `clock` parameter default `time.monotonic` hai, lekin
  test me hum apna fake clock de sakte hain (ise **dependency injection**
  kehte hain - bahut important concept, Part 5 me aur baat hogi).

- `_refill(now)` - Upar Part 3 wala formula, bas. Elapsed time x rate
  add karo, capacity pe cap lagao, `_last_refill` update karo. Dhyan do:
  elapsed zero ya negative ho to kuch nahi karte - clock peeche ja hi
  nahi sakta monotonic hone ki wajah se, phir bhi safety hai.

- `allow(cost=1)` - Sabse important function. Order ye hai:
  1. Lock lo (thread safety, neeche samjhaya hai).
  2. `_refill()` se tokens taaza karo.
  3. cost > capacity hai? To kabhi allow nahi - denied, retry = infinity.
  4. tokens >= cost hai? Tokens kato, allowed = True.
  5. Warna denied, aur retry = (cost - tokens) / refill_rate.
  Dhyan do: **denied request par ek bhi token nahi kat ta.** Agar kat
  jata to galat try karne wale ka bucket negative me chala jata aur use
  aur zyada wait karna padta - ye galat behaviour hota.

- `status()` - Lock leke refill karo aur snapshot de do. Token kat ta nahi.

**Thread safety kyun?** Web server ek saath bahut si requests handle
karta hai, alag-alag threads pe. Socho bucket me 1 token hai aur 2
requests ek hi microsecond pe aayi. Bina lock ke dono "1 token hai"
padh lengi, dono allowed ho jayengi - limit toot gayi. Lock is race ko
rokta hai: ek time pe sirf ek hi thread bucket ko chhu sakta hai. Isko
**race condition** kehte hain, aur is project ki test suite me ek test
sirf isi ke liye hai: 20 threads ek saath hamla karte hain aur total
allowed bilkul capacity ke barabar hi hona chahiye, ek bhi zyada nahi.

#### `InMemoryBucketStore` aur `InMemoryRateLimiter`

Ek bucket to ek client ka hai. Lekin clients bahut hain. Isiliye ek
**manager** chahiye jo key (jaise `"apikey:abc123"` ya `"ip:1.2.3.4"`)
se sahi bucket dhoondhe ya naya banaye:

- Store ek dict rakhta hai: key -> TokenBucket. Apna ek alag lock hai taaki
  do threads ek hi key ke do bucket na bana dein.
- `InMemoryRateLimiter.allow(key, cost)` bas itna karta hai:
  `store.get_or_create(key).allow(cost)`. Har key ki apni bucket -
  isiliye ek client ki limit khatam hone se dusre ko farak nahi padta.
  Is property ko **per-key isolation** kehte hain.

**Limitation samjho:** In-memory matlab state server process ke RAM me
hai. Server restart = saari buckets reset (sabko fresh full bucket).
Aur agar tum server ki 4 copies (processes) chalao, to har copy ki apni
buckets - effective limit 4 guna ho jayegi. Production me isiliye
shared store chahiye. Wahi kaam Redis karta hai.

#### `RedisRateLimiter` (optional wala hissa)

Same interface (`allow` / `status`), lekin buckets Redis (ek alag,
tez, in-memory database server) me rehte hain, hash ke roop me:
`tokens` aur `ts` (last timestamp).

Problem: Redis pe "pehle tokens padho, calculate karo, phir likho" -
ye teen steps agar do servers ek saath karein to race condition wapas
aa jayegi. Solution: poora refill + check + consume ek **Lua script**
me likh ke Redis ko bhejte hain. Redis Lua scripts ko atomically
chalata hai - beech me koi dusra client ghush nahi sakta.

Ek aur subtle baat: time ke liye script Redis server ka apna `TIME`
command use karti hai, taaki saari machines ki ghadiyan alag-alag hon
to bhi hisaab ek hi clock se ho.

Dhyan do, `redis` package ek **optional import** hai - file ke upar
`try/except` me import hota hai. Agar redis installed nahi hai, to
in-memory wala hissa phir bhi bilkul sahi chalega; sirf
`RedisRateLimiter` banane pe saaf error milega. Ye pattern ("optional
dependency ko gracefully handle karo") professional code me bahut
common hai.

### 4.2 `main.py` - FastAPI service

#### `Settings` aur `load_settings()`

Configuration environment variables se aati hai (jaise
`RATE_LIMIT_CAPACITY=10`). Ye function unhe padh ke validate karta hai.
Agar koi `RATE_LIMIT_CAPACITY=abc` likh de, to app chup-chaap default
leke chalne ke bajaye **turant clear error** degi aur band ho jayegi:
`ValueError: RATE_LIMIT_CAPACITY must be an integer, got 'abc'`.
Production systems me ye behaviour sahi mana jata hai - galat config
ke saath aadha-adhoora chalna sabse kharab hota hai ("fail fast").

#### `client_key(request)`

Request kis ki hai, ye pehchanna middleware ka pehla kaam hai:

1. `X-API-Key` header hai? To key = us header ki value. (Ek hi office
   ke 50 log ek hi public IP se aa sakte hain - NAT kehte hain - isiliye
   API key IP se behtar pehchan hai.)
2. Nahi hai? To client ka IP address use karo.

Return me bucket key ke saath `key_type` bhi milta hai, taaki API
response me bata sakein ki pehchan kis cheez se hui. Asli API key ki
value response me kabhi wapas nahi bhejte (security ki aadat).

#### `RateLimitMiddleware`

Middleware matlab: har request controller (endpoint function) tak
pahunchne se pehle isme se guzregi. `dispatch()` ka flow:

1. Path `EXCLUDED_PATHS` me hai (`/health`, `/docs` waghera)? To bina
   token kharch kare seedha aage jaane do. Health check kabhi block
   nahi hona chahiye - warna load balancer sochega server mar gaya hai.
2. Client key nikalo, `limiter.allow(key, cost=1)` call karo.
3. Denied? Wahin rok do - endpoint chalega hi nahi. HTTP 429 response
   banao: JSON body me error + retry time, headers me `Retry-After`
   aur `X-RateLimit-*` teeno.
4. Allowed? Endpoint ko chalne do, aur uske response pe bhi
   `X-RateLimit-Limit / Remaining / Reset` headers chipka do, taaki
   client ko hamesha pata rahe uski haalat kya hai.

#### Endpoints

- `GET /health` - Sirf `{"status": "ok"}`. Kabhi limited nahi.
- `GET /api/data` - Demo endpoint. Middleware ispe 1 token lagata hai.
- `POST /api/consume` - `{"cost": 3}` jaisi body leke ek saath zyada
  tokens kharch karta hai. **Ek design baat khaas samjho:** ye endpoint
  middleware ki list se excluded hai aur limit khud check karta hai.
  Kyun? Agar middleware ispe 1 token lagata aur phir endpoint 3 aur
  lagata, to client se 4 kat jate - **double charging**. Jab bhi cost
  variable ho, limit wahi lagni chahiye jahan asli cost pata ho.
  Galat cost (0, negative, capacity se bada) pe 400 error milta hai,
  aur galat calls pe token nahi kat ta.
- `GET /api/limit-status` - Caller ki bucket ki haalat batata hai
  (tokens remaining, capacity, refill rate, full hone me kitna time).
  Ye khud kabhi token nahi khaata - warna "balance check karna" hi
  mehenga pad jata.

#### `create_app(settings)` - factory pattern

Poori app ek function ke andar banti hai jo settings leta hai. Isi
wajah se tests apni settings (chhoti capacity jaise 3) ke saath alag
app bana sakte hain, bina environment variables se ladde. Neeche
`app = create_app()` asli wali app hai jo uvicorn chalata hai.

### 4.3 `tests/test_rate_limiter.py` - bharose ki dukaan

Sabse clever hissa: `FakeClock`. Ek class jo `time.monotonic` ki acting
karti hai, lekin time sirf tab badhta hai jab test `advance(seconds)`
kahe. Isse refill wale tests ke liye **asli wait karna hi nahi padta** -
"1 ghante baad bucket full hoga" wala test bhi milliseconds me khatam.
Aur har baar bilkul same result (deterministic), kabhi flaky nahi.

Khaas tests jo padhne layak hain:

- `test_retry_after_math_for_known_deficit` - capacity 10, rate 2/sec,
  bucket khaali, cost 3 maanga. Deficit = 3, wait = 3/2 = 1.5 second.
  Test exact 1.5 assert karta hai. Maths galat hoti to ye pakad leta.
- `test_bucket_never_exceeds_capacity_after_long_idle` - 10,000 second
  "aage badh" ke bhi tokens 4 ke 4 hi (capacity). min() wala cap
  kaam kar raha hai ya nahi, ye proof hai.
- `test_concurrent_access_never_over_issues` - 20 threads ek barrier
  pe ruk ke ek saath 200 baar try karte hain (capacity 50). Total
  allowed bilkul 50. Ek bhi zyada hota to lock bekaar hai.
- HTTP wale tests `TestClient` se asli requests bhejte hain (bina
  server chalu kiye): pehle 200, phir 429; headers ki mojoodgi;
  `/health` pe 15 baar maar ke bhi 429 nahi; do alag API keys ki
  buckets alag-alag.

---

## Part 5: Khud try karke dekho (exercises)

1. **Refill ka asar haath se dekho.** Server default settings pe chalao.
   `curl` se `/api/data` 10 baar maro - sab 200. 11vi baar 429 milega.
   Ab thik 1 second ruko aur phir maro - 200 milega (1 token refill ho
   gaya). Samjho kyun?
   (Hint: refill_rate default 1/second hai.)

2. **Headers padho.** `curl -i http://127.0.0.1:8000/api/data` chalao
   aur har response me `X-RateLimit-Remaining` ghat ta hua dekho. 429
   wale pe `Retry-After` dekho.

3. **Cost wala khel.** Server ko `RATE_LIMIT_CAPACITY=5` se chalao.
   `/api/limit-status` dekho (5 tokens). Phir
   `POST /api/consume` pe `{"cost": 3}` bhejo. Status phir dekho -
   2 bache honge. Ab `{"cost": 3}` phir bhejo: 429 aayega, kyunki
   2 < 3. Lekin `{"cost": 2}` chal jayega.

4. **Do clients, do buckets.** Ek hi server pe bina header ke requests
   maro (ye IP wali bucket hai) aur `-H "X-API-Key: dost1"` ke saath
   maro. Dono ki counting alag chalegi. `/api/limit-status` dono
   pehchaanon ke liye alag `tokens_remaining` dega.

5. **Config todo.** `RATE_LIMIT_CAPACITY=abc uvicorn main:app` chalao.
   Server start hi nahi hoga aur saaf error milega. Ye "fail fast"
   behaviour khud experience karo.

6. **Soch wala sawal.** Agar tumhe ek OTP endpoint banana ho jahan
   limit "5 try per 10 minute" ho, to capacity aur refill_rate kya
   rakhoge?
   (Ek jawab: capacity = 5, refill_rate = 5/600 per second. Burst me
   5 try allowed, phir tokens bahut dheere bharenge.)

---

## Part 6: Interview Q&A (5 sawal, jawab ke saath)

**Q1. Token bucket aur fixed window me kya farak hai?**
Fixed window ek fixed time slab (jaise har calendar minute) me count
karta hai. Usme boundary problem hai: window ke end pe 100 aur agle
window ke start pe 100 - 2 second me double burst. Token bucket me
tokens continuously refill hote hain, koi boundary hi nahi, isiliye
burst capacity tak smooth allowed hai aur lambi average rate hamesha
refill rate pe capped rehti hai.

**Q2. Rate limiting kahan lagani chahiye - client side ya server side?**
Hamesha server side (ya API gateway pe). Client side ka code user ke
haath me hai, badla ja sakta hai. Server hi ekmatra jagah hai jahan
limit sach me enforce ho sakti hai. Client side pe sirf "politeness"
wali throttling ho sakti hai, security nahi.

**Q3. Tumhara limiter multiple server instances pe sahi chalega?**
In-memory version ke saath nahi - har process ki apni buckets hongi,
effective limit instances ki count se multiply ho jayegi. Isiliye
project me Redis backend hai: saari state ek shared store me, aur
refill + consume Lua script se atomic - koi race condition nahi.

**Q4. 429 ke saath Retry-After kyun dete hain? Client ko kya fayda?**
Bina Retry-After ke client andhere me retry karega - kuch turant
baar-baar try karenge (server pe aur load), kuch bahut der rukenge
(bekaar ka wait). Retry-After exact batata hai ki token kab available
hoga, to ek samajhdar client bilkul sahi time pe ek hi retry karta
hai. Saath me `X-RateLimit-Remaining` dekh ke client apni speed pehle
se ghata bhi sakta hai.

**Q5. Rate limiting aur throttling me kya antar hai?**
Rate limiting ek hard limit hai: limit cross, request reject (429).
Throttling ka matlab hai request ko reject karne ke bajaye **slow**
kar dena - jaise queue me daal ke dheere-dheere process karna (leaky
bucket isi family ka hai). Dono ka maksad server protection hai;
limiting simple aur predictable hai, throttling user experience ko
thoda naram rakhta hai lekin queue/latency ka bojh laata hai.

---

## Aakhri baat

Jab tum ye project kisi ko dikhao ya resume me likho, to sirf
"rate limiter banaya" mat bolo. Ye teen phrases ghuma ke bolo, wahi
asli samajh dikhati hai: **lazy refill with a monotonic clock**,
**per-key isolation with API key falling back to IP**, aur
**atomic check-and-consume (lock in memory, Lua script in Redis)
taaki race condition me limit na toote**. Ye teen cheezein hi is
project ko ek tutorial-copy se alag production-style code banati hain.
