package main

import (
	"bytes"
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"strconv"
	"testing"
	"time"

	"github.com/btcsuite/btcd/btcec/v2"
	"github.com/btcsuite/btcd/btcec/v2/ecdsa"
	"github.com/btcsuite/btcd/chaincfg"
	"github.com/btcsuite/btcd/chaincfg/chainhash"
	"github.com/btcsuite/btcd/wire"
	graphdb "github.com/lightningnetwork/lnd/graph/db"
	"github.com/lightningnetwork/lnd/graph/db/models"
	"github.com/lightningnetwork/lnd/kvdb"
	"github.com/lightningnetwork/lnd/lnwire"
	"github.com/lightningnetwork/lnd/routing/route"
)

func fixtureEdge(t *testing.T) *models.ChannelEdgeInfo {
	t.Helper()
	var keys []*btcec.PrivateKey
	for i := byte(1); i <= 4; i++ {
		key, _ := btcec.PrivKeyFromBytes(bytes.Repeat([]byte{i}, 32))
		keys = append(keys, key)
	}
	if bytes.Compare(keys[0].PubKey().SerializeCompressed(), keys[1].PubKey().SerializeCompressed()) > 0 {
		keys[0], keys[1] = keys[1], keys[0]
	}
	var pubs [4]route.Vertex
	for i, key := range keys {
		copy(pubs[i][:], key.PubKey().SerializeCompressed())
	}
	id := lnwire.ShortChannelID{BlockHeight: 800000, TxIndex: 3, TxPosition: 1}
	ann := &lnwire.ChannelAnnouncement1{ShortChannelID: id, ChainHash: *chaincfg.MainNetParams.GenesisHash,
		Features: lnwire.NewRawFeatureVector(), NodeID1: pubs[0], NodeID2: pubs[1],
		BitcoinKey1: pubs[2], BitcoinKey2: pubs[3], ExtraOpaqueData: []byte{1, 1, 42}}
	body, err := ann.DataToSign()
	if err != nil {
		t.Fatal(err)
	}
	digest := chainhash.DoubleHashB(body)
	var signatures [4][]byte
	for i, key := range keys {
		signatures[i] = ecdsa.Sign(key, digest).Serialize()
	}
	edge, err := models.NewV1Channel(id.ToUint64(), ann.ChainHash, pubs[0], pubs[1],
		&models.ChannelV1Fields{BitcoinKey1Bytes: pubs[2], BitcoinKey2Bytes: pubs[3], ExtraOpaqueData: ann.ExtraOpaqueData},
		models.WithChannelPoint(wire.OutPoint{Hash: chainhash.Hash{1}, Index: 1}),
		models.WithChanProof(models.NewV1ChannelAuthProof(signatures[0], signatures[1], signatures[2], signatures[3])))
	if err != nil {
		t.Fatal(err)
	}
	return edge
}

func TestDatabaseRoundTripIsReadOnly(t *testing.T) {
	dir := t.TempDir()
	db, err := kvdb.GetBoltBackend(&kvdb.BoltBackendConfig{DBPath: dir, DBFileName: "graph.db", DBTimeout: time.Second})
	if err != nil {
		t.Fatal(err)
	}
	store, err := graphdb.NewKVStore(db)
	if err != nil {
		t.Fatal(err)
	}
	edge := fixtureEdge(t)
	sourceNode := models.NewV1Node(edge.NodeKey1Bytes, &models.NodeV1Fields{
		Features:   lnwire.NewRawFeatureVector(),
		LastUpdate: time.Unix(1, 0),
	})
	if err := store.SetSourceNode(context.Background(), sourceNode); err != nil {
		t.Fatal(err)
	}
	if err := store.AddChannelEdge(context.Background(), edge); err != nil {
		t.Fatal(err)
	}
	// Only node 1 has a stored update; node 2 must export as null.
	policy := &models.ChannelEdgePolicy{Version: lnwire.GossipVersion1, ChannelID: edge.ChannelID,
		LastUpdate: time.Unix(1794000000, 0), MessageFlags: 1, TimeLockDelta: 40, MaxHTLC: 1000,
		ExtraOpaqueData: []byte{1, 0}}
	if _, _, err := store.UpdateEdgePolicy(context.Background(), policy); err != nil {
		t.Fatal(err)
	}
	closedID := lnwire.ShortChannelID{BlockHeight: 700000, TxIndex: 7, TxPosition: 0}
	if err := store.PutClosedScid(context.Background(), closedID); err != nil {
		t.Fatal(err)
	}
	tip := chainhash.Hash{9}
	if _, _, err := store.PruneGraph(context.Background(), nil, &tip, 900000); err != nil {
		t.Fatal(err)
	}
	if err := db.Close(); err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(dir, "graph.db")
	before, _ := hashFile(path)
	output := filepath.Join(dir, "export")
	if err := exportDB(path, output); err != nil {
		t.Fatal(err)
	}
	after, _ := hashFile(path)
	if before != after {
		t.Fatal("database was modified")
	}
	data, err := os.ReadFile(filepath.Join(output, "records.jsonl"))
	if err != nil {
		t.Fatal(err)
	}
	var got record
	if err := json.Unmarshal(data, &got); err != nil {
		t.Fatal(err)
	}
	want, err := exportRecord(edge)
	if err != nil {
		t.Fatal(err)
	}
	if *got.Announcement != *want.Announcement || *got.BitcoinKey1 != *want.BitcoinKey1 || !got.SignatureVerified {
		t.Fatal("signed bytes or keys changed during database round trip")
	}
	if got.SchemaVersion != 2 || got.LastUpdate1 == nil || *got.LastUpdate1 != 1794000000 || got.LastUpdate2 != nil {
		t.Fatal("stored update timestamps were not exported per direction")
	}
	closed, err := os.ReadFile(filepath.Join(output, "closed-scids.txt"))
	if err != nil || string(closed) != strconv.FormatUint(closedID.ToUint64(), 10)+"\n" {
		t.Fatalf("closed-channel index not exported: %q %v", closed, err)
	}
	var exported report
	reportData, _ := os.ReadFile(filepath.Join(output, "export-report.json"))
	if err := json.Unmarshal(reportData, &exported); err != nil || exported.ClosedSCIDs != 1 {
		t.Fatal("closed-channel count missing from report")
	}
	if err := exportDB(path, output); err == nil {
		t.Fatal("overwrote existing export")
	}
	// Optional bridge fixture for Python cross-language validation in test-go.sh.
	if dest := os.Getenv("GOSSIP_TEST_EXPORT"); dest != "" {
		if err := exportDB(path, dest); err != nil {
			t.Fatal(err)
		}
	}
}

func TestCorruptSignatureFails(t *testing.T) {
	edge := fixtureEdge(t)
	edge.ExtraOpaqueData[2] ^= 1
	if _, err := exportRecord(edge); err == nil {
		t.Fatal("accepted corrupted signed field")
	}
}

func TestMissingAndEmptyDatabaseFail(t *testing.T) {
	dir := t.TempDir()
	if err := exportDB(filepath.Join(dir, "absent"), filepath.Join(dir, "out")); err == nil {
		t.Fatal("accepted missing db")
	}
	db, err := kvdb.GetBoltBackend(&kvdb.BoltBackendConfig{DBPath: dir, DBFileName: "empty.db", DBTimeout: time.Second})
	if err != nil {
		t.Fatal(err)
	}
	if err := db.Close(); err != nil {
		t.Fatal(err)
	}
	if err := exportDB(filepath.Join(dir, "empty.db"), filepath.Join(dir, "out")); err == nil {
		t.Fatal("accepted empty db")
	}
}
