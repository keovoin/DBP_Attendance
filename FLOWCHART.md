# Telegram Attendance Tracker - Process Flowcharts

Visual diagrams of how the system is built, deployed, and used. GitHub renders
the Mermaid diagrams below automatically - just view this file on GitHub.

---

## 1. System architecture (the big picture)

```mermaid
flowchart LR
    subgraph User["Team Member"]
        TG["Telegram App"]
    end

    subgraph Fly["Fly.io (always-on Machine)"]
        BOT["Attendance Bot<br/>(python worker)"]
        DB[("SQLite DB<br/>/data/attendance.db<br/>persistent volume")]
    end

    API["Telegram Bot API"]

    TG <-->|messages / commands| API
    BOT -->|long polling: getUpdates| API
    API -->|replies, files| BOT
    BOT <-->|read / write| DB
```

The bot **pulls** messages from Telegram (long polling) and stores everything in
a SQLite database on a persistent disk, so data survives restarts.

---

## 2. Deployment pipeline (how code gets live)

```mermaid
flowchart LR
    DEV["Code pushed to<br/>GitHub branch"] --> GHA["GitHub Actions<br/>(Deploy to Fly.io)"]
    GHA --> CHK{"Required secrets<br/>present?"}
    CHK -->|No| FAIL["Fail with a clear<br/>message - add secrets"]
    CHK -->|Yes| SEC["Stage secrets on Fly<br/>(BOT_TOKEN, ADMIN_TELEGRAM_IDS)"]
    SEC --> DEP["flyctl deploy"]
    DEP --> RUN["Fly Machine runs<br/>the bot 24/7"]
```

Secrets live in **GitHub → Settings → Secrets** and Fly; they are never in the code.

---

## 3. Command dispatch (what happens when you send anything)

```mermaid
flowchart TD
    IN["Incoming update"] --> TYPE{"What kind?"}
    TYPE -->|Location shared| LOC["Handle On_Site location"]
    TYPE -->|Button tap| CB["Handle clock-in type choice"]
    TYPE -->|Text| CMD{"Starts with '/'?"}
    CMD -->|No| CONV{"In a conversation?<br/>(e.g. awaiting coordinator)"}
    CONV -->|Yes| CONT["Continue that step"]
    CONV -->|No| HINT["Reply: send /help"]
    CMD -->|Yes| ROUTE["Route to the matching command<br/>register / clockin / clockout /<br/>remark / view / export /<br/>setlocation / promote / help"]
```

---

## 4. Registration flow (`/register`)

```mermaid
flowchart TD
    R["/register"] --> REG{"Already<br/>registered?"}
    REG -->|Yes| KEEP["Reply 'already registered'<br/>keep existing record"]
    REG -->|No| ADMIN{"Telegram ID in<br/>ADMIN_TELEGRAM_IDS?"}
    ADMIN -->|Yes| MKADMIN["Create member as ADMIN"]
    ADMIN -->|No| MKUSER["Create member as REGULAR user"]
    MKADMIN --> ASK["Ask for coordinator name"]
    MKUSER --> ASK
    ASK --> WAIT["Wait for reply"]
    WAIT --> SAVE["Save coordinator"]
    SAVE --> DONE["Registered and ready"]
```

---

## 5. Clock-in flow (`/clockin`) - the core feature

```mermaid
flowchart TD
    CI["/clockin"] --> MEMBER{"Registered?"}
    MEMBER -->|No| RNO["Ask to /register"]
    MEMBER -->|Yes| OPEN{"Already have an open<br/>(not clocked-out) entry today?"}
    OPEN -->|Yes| REJ1["Reject: clock-in already exists"]
    OPEN -->|No| COORD{"Coordinator set?"}
    COORD -->|No| ASKC["Ask for coordinator,<br/>then continue"]
    COORD -->|Yes| PICK["Show buttons:<br/>Remote / On_Site"]
    ASKC --> PICK

    PICK --> CHOICE{"Which type?"}

    CHOICE -->|Remote| REMOTE["Record entry<br/>type = Remote<br/>+ timestamp + coordinator"]
    REMOTE --> OKR["Confirm clock-in"]

    CHOICE -->|On_Site| CONF{"On-site location<br/>configured by admin?"}
    CONF -->|No| REJ2["Reject: no on-site<br/>location configured"]
    CONF -->|Yes| REQ["Ask member to<br/>share location"]
    REQ --> DIST["Measure distance to<br/>configured location"]
    DIST --> RANGE{"Within 20 meters?"}
    RANGE -->|No| REJ3["Reject: outside<br/>20-meter range"]
    RANGE -->|Yes| ONSITE["Record entry<br/>type = On_Site<br/>+ timestamp + coordinator<br/>+ GPS location"]
    ONSITE --> OKO["Confirm clock-in"]
```

---

## 6. Clock-out flow (`/clockout`)

```mermaid
flowchart TD
    CO["/clockout"] --> M{"Registered?"}
    M -->|No| N["Ask to /register"]
    M -->|Yes| OPEN{"Open entry<br/>for today?"}
    OPEN -->|No| REJ["Reject: no active<br/>clock-in today"]
    OPEN -->|Yes| REC["Record clock-out time"]
    REC --> OK["Confirm clock-out"]
```

---

## 7. Late remark flow (`/remark <date> <text>`)

```mermaid
flowchart TD
    RM["/remark 2026-07-08 reason..."] --> VAL{"Valid date and<br/>text given?"}
    VAL -->|No| USAGE["Show usage example"]
    VAL -->|Yes| EXIST{"Attendance entry<br/>on that date?"}
    EXIST -->|No| REJ["Reject: no record<br/>for that date"]
    EXIST -->|Yes| SET["Save remark<br/>(replaces any old one)"]
    SET --> OK["Confirm saved"]
```

---

## 8. View / Export with role-based access (`/view`, `/export`)

```mermaid
flowchart TD
    V["/view or /export<br/>(optional: member, date range)"] --> ROLE{"Who is asking?"}

    ROLE -->|Regular user| SELF["Return ONLY their<br/>own records"]
    ROLE -->|Admin| WHO{"Specified a<br/>member?"}
    WHO -->|Yes| ONE["Return that member's records"]
    WHO -->|No| ALL["Return ALL members' records"]

    SELF --> RANGE["Apply date range<br/>if provided"]
    ONE --> RANGE
    ALL --> RANGE
    RANGE --> OUT{"/view or /export?"}
    OUT -->|/view| TEXT["Send as a text summary"]
    OUT -->|/export| CSV["Send as a downloadable<br/>CSV file"]
```

> A regular user asking for someone else's records is always redirected to their
> own - that is the role-based access rule.

---

## 9. Admin-only actions

```mermaid
flowchart TD
    A["/setlocation lat lon<br/>or /promote member"] --> ADM{"Is the requester<br/>an Admin?"}
    ADM -->|No| DENY["Reject: requires Admin access"]
    ADM -->|Yes| ACT{"Which action?"}
    ACT -->|setlocation| LOC["Save on-site location<br/>(used for all future<br/>On_Site clock-ins)"]
    ACT -->|promote| PRO["Grant Admin role<br/>to the target member"]
```

---

## Data recorded per attendance entry

| Field | Example |
| --- | --- |
| Member | Alice User |
| Date | 2026-07-08 |
| Clock-in time | 2026-07-08 09:02:11 |
| Clock-out time | 2026-07-08 17:30:04 |
| Type | Remote / On_Site |
| Coordinator | Bob |
| Location (On_Site only) | 11.5564, 104.9282 |
| Late remark | "Traffic delay" |
