# Drive structure

```text
Audiobook Drive Root/
├── Audiobook Videos/
│   ├── Folder A/
│   │   ├── clip1.mp4
│   │   └── clip2.mp4
│   ├── Folder B/
│   ├── Folder C/
│   └── Folder D/
├── jobs/
├── queues/
└── state/
```

The reusable video library is read-only from the production pipeline. Jobs download clips temporarily; they are not uploaded into the job snapshot.
