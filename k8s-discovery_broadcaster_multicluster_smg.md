Now let me search for information about the mesh communication and multi-cluster support:Perfect! Now I have comprehensive information. Let me create a detailed explanation with examples.

## Service Discovery (K8s Informer) & Event Broadcaster in SMG

Now let me explain the advanced concepts of **Kubernetes Service Discovery Informer** and **Event Broadcaster**, plus how multiple SMGs communicate across clusters.

---

## **1. Kubernetes Informer-Based Service Discovery**

SMG uses Kubernetes **Informers** (watch API) to automatically discover and track worker pods. This is a "pull when changes happen" model rather than polling.

### Architecture Flow

```
┌─────────────────────────────────────────────────────────────┐
│                   Kubernetes API Server                      │
│                    (Source of Truth)                         │
└────────────────────────┬────────────────────────────────────┘
                         │
                         ↓
            ┌────────────────────────────┐
            │  K8s Watcher (Informer)    │
            │  Watches pods with label:  │
            │  app=sglang-worker         │
            └────────────┬───────────────┘
                         │
         ┌───────────────┴───────────────┐
         │                               │
         ↓                               ↓
   ┌──────��───────┐          ┌───────────────────┐
   │ Pod Created  │          │ Pod Deleted       │
   │ Event        │          │ Event             │
   └──────┬───────┘          └────────┬──────────┘
          │                           │
          ↓                           ↓
   ┌────────────────────────────────────────────┐
   │   Event Broadcaster                         │
   │   (Triggers handler functions)              │
   └────────────────────────────────────────────┘
          │                           │
          ↓                           ↓
  ┌──────────────────┐     ┌──────────────────┐
  │handle_pod_event  │     │handle_pod_deletion│
  │Creates           │     │Creates            │
  │AddWorker Job     │     │RemoveWorker Job   │
  └────────┬─────────┘     └────────┬──────────┘
           │                        │
           ↓                        ↓
    ┌──────────────────────────────────────┐
    │    JobQueue (Async Processing)       │
    │    • Worker Registration              │
    │    • Health Checks                    │
    │    • Capability Query                 │
    └──────────────────────────────────────┘
           │
           ↓
    ┌──────────────────────────────────┐
    │   Worker Registry                 │
    │   • Tracks all workers            │
    │   • By model, type, connection    │
    │   • Updates hash rings            │
    └──────────────────────────────────┘
```

### Service Discovery Configuration

```bash name=service_discovery_config.sh
smg \
  --service-discovery \
  --selector app=sglang-worker \                # Label selector for pods
  --service-discovery-namespace inference \   # K8s namespace to watch
  --service-discovery-port 8000 \             # Port for worker connections
  --service-discovery-protocol http            # Protocol: http or grpc
```

### Code: K8s Informer Setup

```rust name=service_discovery.rs url=https://github.com/lightseekorg/smg/blob/b8f25a671f3a4f9e95daa3c37e93d3ec42d008bf/model_gateway/src/service_discovery.rs#L306-L448
pub async fn start_service_discovery(
    config: ServiceDiscoveryConfig,
    app_context: Arc<AppContext>,
    mesh_cluster_state: Option<ClusterState>,
    mesh_port: Option<u16>,
) -> Result<task::JoinHandle<()>, kube::Error> {
    if !config.enabled {
        return Err(kube::Error::Api(...));
    }

    let client = Client::try_default().await?;

    // Creates K8s watcher informer for pods matching selector
    let api = Api::<Pod>::namespaced(client, &namespace);
    let watcher = watcher(api, ListParams::default().labels(&label_selector));

    // Watch loop - informer pattern
    // Triggers handlers on ADDED, MODIFIED, DELETED events
    match watcher.try_for_each(move |event| {
        // Event handler processes pod changes
        match event {
            Event::Apply(pod) => {
                // Pod created or modified
                if pod.metadata.deletion_timestamp.is_some() {
                    // Pod is terminating
                    handle_pod_deletion(&pod_info, tracked_pods, app_context, port).await;
                } else {
                    // Pod added/updated
                    handle_pod_event(&pod_info, tracked_pods, app_context, port, pd_mode).await;
                }
            }
            Event::Delete(pod) => {
                // Pod deleted
                handle_pod_deletion(&pod_info, tracked_pods, app_context, port).await;
            }
            _ => {}
        }
    }).await {
        Ok(()) => { /* Continue watching */ }
        Err(e) => {
            error!("Kubernetes watcher error: {}", e);
            // Exponential backoff retry
        }
    }
}
```

---

## **2. Event Broadcaster & Handler Functions**

The **Event Broadcaster** is the mechanism that processes K8s events and converts them into SMG operations.

### Pod Addition Event Flow

```rust name=handle_pod_event.rs url=https://github.com/lightseekorg/smg/blob/b8f25a671f3a4f9e95daa3c37e93d3ec42d008bf/model_gateway/src/service_discovery.rs#L479-L598
async fn handle_pod_event(
    pod_info: &PodInfo,
    tracked_pods: Arc<Mutex<HashSet<PodInfo>>>,
    app_context: Arc<AppContext>,
    port: u16,
    pd_mode: bool,
) {
    let worker_url = pod_info.worker_url(port);  // e.g., http://10.0.0.5:8000

    if pod_info.is_healthy() {
        // 1. Check if pod is already tracked
        let (should_add, tracked_count) = {
            let mut tracker = tracked_pods.lock().unwrap();
            if tracker.contains(pod_info) {
                (false, tracker.len())  // Duplicate event
            } else {
                tracker.insert(pod_info.clone());
                (true, tracker.len())   // New pod
            }
        };

        if should_add {
            info!("Adding pod: {} | type: {:?} | url: {}", 
                  pod_info.name, pod_info.pod_type, worker_url);

            // 2. Determine worker type (Prefill/Decode/Regular)
            let worker_type = if pd_mode {
                match &pod_info.pod_type {
                    Some(PodType::Prefill) => WorkerType::Prefill,
                    Some(PodType::Decode) => WorkerType::Decode,
                    _ => WorkerType::Regular,
                }
            } else {
                WorkerType::Regular
            };

            // 3. Build worker spec
            let mut spec = WorkerSpec::new(worker_url.clone());
            spec.worker_type = worker_type;
            spec.bootstrap_port = pod_info.bootstrap_port;  // For PD mode
            spec.api_key.clone_from(&app_context.router_config.api_key);

            // 4. Create AddWorker job
            let job = Job::AddWorker {
                config: Box::new(spec),
            };

            // 5. Submit to JobQueue (async, non-blocking)
            if let Some(job_queue) = app_context.worker_job_queue.get() {
                match job_queue.submit(job).await {
                    Ok(()) => {
                        debug!("Worker addition job submitted for: {}", worker_url);
                        Metrics::record_discovery_registration(
                            metrics_labels::DISCOVERY_KUBERNETES,
                            metrics_labels::REGISTRATION_SUCCESS,
                        );
                    }
                    Err(e) => {
                        error!("Failed to submit job: {}", e);
                        // Remove from tracking on failure
                        if let Ok(mut tracker) = tracked_pods.lock() {
                            tracker.remove(pod_info);
                        }
                    }
                }
            }
        }
    }
}
```

### Pod Deletion Event Flow

```rust name=handle_pod_deletion.rs url=https://github.com/lightseekorg/smg/blob/b8f25a671f3a4f9e95daa3c37e93d3ec42d008bf/model_gateway/src/service_discovery.rs#L633-L664
async fn handle_pod_deletion(
    pod_info: &PodInfo,
    tracked_pods: Arc<Mutex<HashSet<PodInfo>>>,
    app_context: Arc<AppContext>,
    port: u16,
) {
    let worker_url = pod_info.worker_url(port);

    // 1. Remove from tracking
    let (should_remove, remaining_count) = {
        let mut tracker = tracked_pods.lock().unwrap();
        if tracker.contains(pod_info) {
            tracker.remove(pod_info);
            (true, tracker.len())
        } else {
            (false, tracker.len())
        }
    };

    if should_remove {
        info!("Removing pod: {} | url: {}", pod_info.name, worker_url);

        // 2. Create RemoveWorker job
        let job = Job::RemoveWorker {
            worker_urls: vec![worker_url.clone()],
        };

        // 3. Submit to JobQueue
        if let Some(job_queue) = app_context.worker_job_queue.get() {
            if let Err(e) = job_queue.submit(job).await {
                error!("Failed to submit removal job: {}", e);
            } else {
                debug!("Submitted worker removal job");
                Metrics::record_discovery_deregistration(
                    metrics_labels::DISCOVERY_KUBERNETES,
                    metrics_labels::DEREGISTRATION_POD_DELETED,
                );
                
                // Update metrics
                Metrics::set_discovery_workers_discovered(
                    metrics_labels::DISCOVERY_KUBERNETES,
                    remaining_count,
                );
            }
        }
    }
}
```

### Practical Example: Pod Scaling

```
SCENARIO: Scaling up sglang-worker from 2 to 4 replicas
==========================================================

Kubernetes Event Timeline:
─────────────────────────

1️⃣  Time: T=0s
    Kubernetes creates pod: sglang-worker-2
    ├─ Pod IP: 10.0.0.7
    ├─ Status: Pending
    └─ Labels: app=sglang-worker,model=llama-3-8b

2️⃣  Time: T=1s
    Informer receives ADDED event
    ├─ Calls handle_pod_event()
    ├─ Checks if healthy (status=Running, ready=True)
    └─ Pod is healthy ✓

3️⃣  Time: T=1.2s
    EventBroadcaster publishes AddWorker job
    ├─ worker_url: http://10.0.0.7:8000
    ├─ worker_type: Regular
    └─ Submitted to JobQueue

4️⃣  Time: T=1.5s (Background JobQueue processing)
    ├─ Queries pod's /get_model_info endpoint
    ├─ Verifies it can handle llama-3-8b
    ├─ Creates Worker object
    └─ Registers in WorkerRegistry

5️⃣  Time: T=1.7s
    Registry updates all indices:
    ├─ workers map: WorkerId -> Worker
    ├─ model_index: "llama-3-8b" -> [worker0, worker1, worker2]  // NEW
    ├─ type_workers: Regular -> [id0, id1, id2]                  // NEW
    ├─ Rebuilds hash ring for llama-3-8b
    └─ Starts health checks on new worker

6️⃣  Time: T=2s
    New pod handles traffic:
    ├─ Requests for llama-3-8b routed to all 3 workers
    ├─ Hash ring: ~1/3 requests → worker2
    ├─ Load balanced across prefill & decode
    └─ Metrics updated: worker_count = 3

---

7️⃣  Time: T=10s
    Kubernetes creates pod: sglang-worker-3
    └─ Repeats steps 1-6 above
```

---

## **3. Multi-Cluster SMG Communication via Mesh**

When you have **multiple SMG instances across different clusters**, they communicate through the **Mesh Module** using a **Gossip Protocol**.

### Multi-Cluster Architecture

```
┌──────────────────────────────────────────────────────────────┐
│                      CLUSTER A                               │
│  ┌────────────────┐         ┌──────────────────────┐        │
│  │  SMG Router 1  │         │   Service Discovery  │        │
│  │  (gRPC: 9000)  │         │   (Watches pods)     │        │
│  │                │         │                      │        │
│  │  Worker        │         │   ┌──────────┐      │        │
│  │  Registry      │         │   │pod-add   │──┐   │        │
│  │                │         │   │pod-del   │  │   │        │
│  └────────┬───────┘         │   └──────────┘  │   │        │
│           │                 └────────────┬────┘   │        │
│           │                              │        │        │
│    ┌──────▼──────────┐          ┌────────▼────┐  │        │
│    │  Mesh Service   │          │ JobQueue    │  │        │
│    │  (Gossip)       │          │ (Process    │  │        │
│    │  Listening on   │          │  jobs)      │  │        │
│    │  :3001 (HA)     │          └──────┬─────┘  │        │
│    └────────┬────────┘                 │        │        │
│             │                    ┌─────▼──────┐ │        │
│             │                    │Worker Reg  │ │        │
│             │                    │(Add/Remove)│ │        │
│             │                    └────────────┘ │        │
└─────────────┼────────────────────────────────────┘        │
              │                                              │
              │          GOSSIP PROTOCOL                     │
              │  (gRPC state sync)                           │
              │          (Every ~100ms)                      │
              │                                              │
┌─────────────▼────────────────────────────────────────────┐│
│                      CLUSTER B                           ││
│  ┌────────────────┐         ┌──────────────────────┐    ││
│  │  SMG Router 2  │         │   Service Discovery  │    ││
│  │  (gRPC: 9000)  │         │   (Watches pods)     │    ││
│  │                │         │                      │    ││
│  │  Worker        │         │   ┌──────────┐      │    ││
│  │  Registry      │         │   │pod-add   │──┐   │    ││
│  │  (SYNCED)      │         │   │pod-del   │  │   │    ││
│  └────────────────┘         │   └──────────┘  │   │    ││
│           ▲                 └────────────┬────┘   │    ││
│           │                              │        │    ││
│    ┌──────┴──────────┐          ┌────────▼────┐  │    ││
│    │  Mesh Service   │          │ JobQueue    │  │    ││
│    │  (Gossip)       │          │ (Process    │  │    ││
│    │  Listening on   │          │  jobs)      │  │    ││
│    │  :3001 (HA)     │          └──────┬─────┘  │    ││
│    └────────────────┘                 │        │    ││
│                                  ┌─────▼──────┐ │    ││
│                                  │Worker Reg  │ │    ││
│                                  │ (Receives  │ │    ││
│                                  │  sync)     │ │    ││
│                                  └────────────┘ │    ││
└──────────────────────────────────────────────────┘    │
└───────────────────────────────────────────────────────┘
```

### Mesh Module: State Synchronization

```rust name=mesh_sync.rs url=https://github.com/lightseekorg/smg/blob/b8f25a671f3a4f9e95daa3c37e93d3ec42d008bf/mesh/src/sync.rs#L18-L23
/// Mesh sync manager for coordinating state synchronization
#[derive(Clone, Debug)]
pub struct MeshSyncManager {
    pub(crate) stores: Arc<StateStores>,  // CRDT stores for eventual consistency
    self_name: String,                     // Unique node identifier
}
```

### Key Mesh Concepts

#### **1. ClusterState - Distributed Node Registry**

```rust name=cluster_state.rs url=https://github.com/lightseekorg/smg/blob/b8f25a671f3a4f9e95daa3c37e93d3ec42d008bf/mesh/src/service.rs#L1-L40
pub type ClusterState = Arc<RwLock<BTreeMap<String, NodeState>>>;

// Each node stores metadata about cluster members
struct NodeState {
    name: String,                    // e.g., "smg-router-1"
    address: SocketAddr,             // e.g., 192.168.1.10:9000
    status: NodeStatus,              // Alive | Suspected | Down
    version: u64,                    // Lamport clock for ordering
    workers: Vec<WorkerInfo>,        // Workers registered on this node
}
```

#### **2. Gossip Protocol - Peer-to-Peer Sync**

```
How worker registrations sync across clusters:

Node A (Cluster-A):                 Node B (Cluster-B):
┌──────────────────┐               ┌──────────────────┐
│ Worker: llama-8b │               │ (waiting)        │
│ added locally    │               └──────────────────┘
└────────┬─────────┘
         │
    Gossip tick (every ~100ms)
         │
         ├─→ Sends: "AddWorker(llama-8b, http://10.0.0.7:8000)"
         │
         ├─→ Receives: ACK from Node B
         │
         └─→ Node B applies state update
             │
             ├─ Updates ClusterState
             ├─ Creates worker object
             ├─ Adds to Worker Registry
             └─ Ready to serve requests
```

#### **3. Topology Modes**

```rust name=topology.rs url=https://github.com/lightseekorg/smg/blob/b8f25a671f3a4f9e95daa3c37e93d3ec42d008bf/mesh/src/topology.rs#L21-L49
pub struct TopologyConfig {
    pub full_mesh_threshold: usize,    // Default: 10 nodes
    pub region: Option<String>,        // e.g., "us-east-1"
    pub availability_zone: Option<String>,  // e.g., "us-east-1a"
}

// Full Mesh (≤ 10 nodes):
// All nodes connect to all other nodes
// │ Node-A ←→ Node-B
// │ Node-A ←→ Node-C
// │ Node-B ←→ Node-C
// Maximum redundancy, high bandwidth

// Sparse Mesh (> 10 nodes):
// Nodes connect by region/AZ
// │ Node-A (us-east-1a) → Node-B (us-east-1a)
// │ Node-A (us-east-1a) → Node-D (us-west-2a)
// Reduced connections, efficient for large clusters
```

### Multi-Cluster Example Scenario

```
SCENARIO: 2 Clusters with Service Discovery + Mesh
======================================================

Initial State:
─────────────
Cluster A: 1 SMG (Router A), 2 worker pods
Cluster B: 1 SMG (Router B), 2 worker pods

All pods: label app=llama-worker

─────────────────────────────────────────────

Step 1: Node A discovers its local workers
────────────────────────────────────────
Cluster A's Service Discovery:
├─ Watches pods with label app=llama-worker
├─ Discovers: pod-a-1 (10.0.0.1:8000) → AddWorker job
├─ Discovers: pod-a-2 (10.0.0.2:8000) → AddWorker job
└─ Worker Registry A:
   ├─ model_index["llama-3-8b"] = [worker-1, worker-2]
   └─ hash_ring["llama-3-8b"] = 2 workers

Step 2: Node B discovers its local workers
────────────────────────────────────────
Cluster B's Service Discovery:
├─ Watches pods with label app=llama-worker
├─ Discovers: pod-b-1 (10.0.0.3:8000) → AddWorker job
├─ Discovers: pod-b-2 (10.0.0.4:8000) → AddWorker job
└─ Worker Registry B:
   ├─ model_index["llama-3-8b"] = [worker-3, worker-4]
   └─ hash_ring["llama-3-8b"] = 2 workers

Step 3: Mesh sync begins
────────────────────────
Node A (Mesh Service):
├─ Gossip to Node B: "I have 2 workers on llama-3-8b"
├─ Sends: WorkerState batch
│  ├─ worker-1: http://10.0.0.1:8000
│  └─ worker-2: http://10.0.0.2:8000
└─ mesh_sync.sync_worker_state() called

Node B receives sync:
├─ Updates mesh_cluster_state
├─ WorkerRegistry.register() is called for worker-1, worker-2
├─ B's model_index["llama-3-8b"] now includes ALL 4 workers:
│  └─ [worker-1, worker-2, worker-3, worker-4]
├─ Rebuilds hash ring with 4 nodes
└─ Metrics: "4 nodes discovered across cluster"

Step 4: Client requests load balance globally
──────────────────────────────────────────────
Client sends inference request to Node A or Node B:

Router A:
├─ Receives request
├─ Queries: get_by_model("llama-3-8b")
│  └─ Arc clone of 4-worker snapshot (FROM MESH SYNC)
├─ Hash rings consistent across both nodes
├─ Routes to: worker-2 (could be local or remote)
└─ If remote (cluster B): gRPC to Node B's worker-2

Router B:
├─ Also sees all 4 workers (from gossip sync)
├─ Can route to any of 4 workers locally or remotely
└─ Even distribution across clusters

Step 5: Scale up in Cluster A
─────────────────────────────
New pod: pod-a-3 (10.0.0.5:8000)

Node A:
├─ Service Discovery detects new pod
├─ Submits AddWorker job → Added to registry
├─ Mesh syncs: "I now have 3 workers"

Node B:
├─ Receives gossip update
├─ Adds worker-5 to its registry
├─ Now has 5 total workers: [1,2,3,4,5]
├─ Rebuilds hash ring (5-node ring)
└─ New traffic distribution: ~20% to each worker
```

### How Multiple SMGs Stay in Sync

```rust name=mesh_gossip.rs url=https://github.com/lightseekorg/smg/blob/b8f25a671f3a4f9e95daa3c37e93d3ec42d008bf/mesh/src/ping_server.rs#L238-L267
impl GossipService {
    pub fn new(state: ClusterState, self_addr: SocketAddr, self_name: &str) -> Self {
        Self {
            state,  // BTreeMap<NodeName, NodeState>
            self_addr,
            self_name: self_name.to_string(),
            stores: None,  // CRDT stores for eventual consistency
            sync_manager: None,  // MeshSyncManager for applying updates
            // ...
        }
    }

    // Called periodically (gossip tick)
    fn merge_state(&self, incoming_nodes: Vec<NodeState>) -> bool {
        // CRDT merge: incoming state + local state = consistent result
        // Updates are idempotent and commutative
        // Eventually consistent - all nodes converge to same state
    }
}
```

### Mesh Integration with Service Discovery

```
Service Discovery + Mesh = Distributed Worker Registry
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Each SMG Instance:
┌──────────────────────────────────────────────┐
│                                              │
│  ┌─────────────────────────────────────┐    │
│  │ Service Discovery (K8s Informer)     │    │
│  │ • Watches local Kubernetes pods      │    │
│  │ • Fires AddWorker/RemoveWorker jobs  │    │
│  └────────────┬────────────────────────┘    │
│               │                              │
│               ↓                              │
│  ┌─────────────────────────────────────┐    │
│  │ JobQueue (Process async)             │    │
│  │ • Registers worker in local registry │    │
│  │ • Calls mesh_sync.sync_worker_state()│    │
│  └────────────┬────────────────────────┘    │
│               │                              │
│               ↓                              │
│  ┌─────────────────────────────────────┐    │
│  │ Worker Registry (Local)              │    │
│  │ • model_index, hash_rings            │    │
│  │ • Updated with mesh sync             │    │
│  └────────────┬────────────────────────┘    │
│               │                              │
│               ↓                              │
│  ┌─────────────────────────────────────┐    │
│  │ Mesh Sync Manager                    │    │
│  │ • Broadcasts: "AddWorker(...)"       │    │
│  │ • Via gossip to other SMG instances  │    │
│  │ • Other nodes receive & apply sync   │    │
│  └──────────────┬──────────────────────┘    │
│                │                             │
│                ↓                             │
│  ┌──────────────────────────────────────┐   │
│  │ Gossip Protocol (gRPC)               │   │
│  │ • Full mesh (≤10 nodes)              │   │
│  │ • Sparse mesh (>10 nodes, by AZ)     │   │
│  │ • Eventual consistency               │   │
│  └──────────────────────────────────────┘   │
│                │                             │
└────────────────┼─────────────────────────────┘
                 │
    ┌────────────┴────────────┐
    ↓                         ↓
 Other SMG             Other SMG
 Instance B            Instance C
 (Receives             (Receives
  sync)                 sync)
```

---

## **Summary**

| Component | Purpose | Mechanism |
|-----------|---------|-----------|
| **K8s Informer** | Watch pod changes | API watch, triggers on create/delete/modify |
| **Event Broadcaster** | Convert K8s events to actions | `handle_pod_event()`, `handle_pod_deletion()` |
| **JobQueue** | Async worker registration | Background processing, non-blocking |
| **Worker Registry** | Track all workers | Multiple indices (model, type, connection, URL) |
| **Mesh Service** | Multi-cluster sync | Gossip protocol, eventual consistency (CRDT) |
| **Topology Manager** | Optimize peer connections | Full mesh (≤10) or sparse mesh (>10 nodes) |
| **MeshSyncManager** | Propagate state changes | Broadcasts worker add/remove/health updates |

### Key Advantages

✅ **Automatic Discovery** - K8s informer replaces manual worker URL management  
✅ **Real-time Sync** - Pod events trigger immediate registry updates  
✅ **Global Load Balancing** - All SMGs see all workers (via mesh sync)  
✅ **Eventual Consistency** - CRDT ensures convergence without consensus  
✅ **High Availability** - Multiple SMGs can serve same requests  
✅ **Scalable** - Gossip protocol scales to hundreds of nodes (sparse mesh)  
✅ **Region-Aware** - Sparse mesh groups nodes by region/AZ to reduce latency  

This architecture enables **transparent, zero-downtime scaling** across multiple Kubernetes clusters with minimal operational overhead!
