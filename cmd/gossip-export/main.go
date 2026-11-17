// gossip-export reads an OFFLINE COPY of the pinned LND Bolt graph.
// Build inside the pinned LND module, never against an arbitrary latest version.
package main

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"time"

	"github.com/btcsuite/btcd/chaincfg"
	graphdb "github.com/lightningnetwork/lnd/graph/db"
	"github.com/lightningnetwork/lnd/graph/db/models"
	"github.com/lightningnetwork/lnd/kvdb"
	"github.com/lightningnetwork/lnd/lnwire"
	"github.com/lightningnetwork/lnd/netann"
)

const lndCommit = "390b2d2b7bdcc335c183962e483f5daeceb8f0e7"

// Version 2 adds the stored channel_update timestamps and the closed-channel index.
const schemaVersion = 2

// closedScidBucket mirrors the unexported top-level bucket of the pinned graph
// store. The round-trip test writes through PutClosedScid, so an upstream rename
// fails the build instead of silently reporting an empty index.
var closedScidBucket = []byte("closed-scid")

type record struct {
	SchemaVersion       int     `json:"schema_version"`
	Provenance          string  `json:"provenance"`
	GossipVersion       int     `json:"gossip_version"`
	ChainHash           string  `json:"chain_hash"`
	SCID                string  `json:"scid"`
	FundingOutpoint     string  `json:"funding_outpoint"`
	NodeKey1            string  `json:"node_key_1"`
	NodeKey2            string  `json:"node_key_2"`
	BitcoinKey1         *string `json:"bitcoin_key_1"`
	BitcoinKey2         *string `json:"bitcoin_key_2"`
	Announcement        *string `json:"announcement_hex"`
	SignatureVerified   bool    `json:"signature_verified"`
	FundingConstruction string  `json:"funding_construction"`
	ExclusionReason     *string `json:"exclusion_reason"`
	// Timestamps of the stored channel_update of node 1 and node 2, as recorded
	// by this observer's LND. The announcement signatures do not cover them.
	LastUpdate1 *int64 `json:"last_update_1"`
	LastUpdate2 *int64 `json:"last_update_2"`
}

type blockRef struct {
	Height uint32 `json:"height"`
	Hash   string `json:"hash"`
}

type report struct {
	SchemaVersion   int            `json:"schema_version"`
	LNDCommit       string         `json:"lnd_commit"`
	DatabaseSHA256  string         `json:"database_sha256"`
	RecordsSHA256   string         `json:"records_sha256"`
	GraphPruneTip   blockRef       `json:"graph_prune_tip"`
	ExportedRecords int            `json:"exported_records"`
	IncludedRecords int            `json:"included_records"`
	Exclusions      map[string]int `json:"exclusions"`
	// Channels this observer marked closed and therefore never exported.
	ClosedSCIDs       int    `json:"closed_scids"`
	ClosedSCIDsSHA256 string `json:"closed_scids_sha256"`
}

func hashFile(path string) (string, error) {
	f, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer f.Close()
	h := sha256.New()
	if _, err := io.Copy(h, f); err != nil {
		return "", err
	}
	return hex.EncodeToString(h.Sum(nil)), nil
}

func updateTime(policy *models.ChannelEdgePolicy) *int64 {
	if policy == nil {
		return nil
	}
	seconds := policy.LastUpdate.Unix()
	return &seconds
}

func closedSCIDs(db kvdb.Backend) ([]uint64, error) {
	var ids []uint64
	err := kvdb.View(db, func(tx kvdb.RTx) error {
		bucket := tx.ReadBucket(closedScidBucket)
		if bucket == nil {
			return nil
		}
		return bucket.ForEach(func(key, _ []byte) error {
			if len(key) != 8 {
				return errors.New("malformed closed-channel index key")
			}
			ids = append(ids, binary.BigEndian.Uint64(key))
			return nil
		})
	}, func() { ids = nil })
	sort.Slice(ids, func(i, j int) bool { return ids[i] < ids[j] })
	return ids, err
}

func exportRecord(edge *models.ChannelEdgeInfo) (record, error) {
	r := record{SchemaVersion: schemaVersion, Provenance: "reconstructed_from_lnd_graph",
		GossipVersion: 1, ChainHash: edge.ChainHash.String(),
		SCID: strconv.FormatUint(edge.ChannelID, 10), FundingOutpoint: edge.ChannelPoint.String(),
		NodeKey1: hex.EncodeToString(edge.NodeKey1Bytes[:]),
		NodeKey2: hex.EncodeToString(edge.NodeKey2Bytes[:]), FundingConstruction: "unknown"}
	if edge.Version != lnwire.GossipVersion1 || edge.ChainHash != *chaincfg.MainNetParams.GenesisHash {
		return r, errors.New("unsupported gossip version or chain")
	}
	if uint64(edge.ChannelPoint.Index) != edge.ChannelID&65535 {
		return r, errors.New("outpoint index differs from SCID")
	}
	key1, err := edge.BitcoinKey1Bytes.UnwrapOrErr(errors.New("missing funding key 1"))
	if err != nil {
		return r, err
	}
	key2, err := edge.BitcoinKey2Bytes.UnwrapOrErr(errors.New("missing funding key 2"))
	if err != nil {
		return r, err
	}
	k1, k2 := hex.EncodeToString(key1[:]), hex.EncodeToString(key2[:])
	r.BitcoinKey1, r.BitcoinKey2 = &k1, &k2
	if edge.AuthProof == nil || edge.AuthProof.IsEmpty() {
		reason := "missing_proof"
		r.ExclusionReason = &reason
		return r, nil
	}
	ann, err := edge.ToChannelAnnouncement()
	if err != nil {
		return r, err
	}
	if err := netann.ValidateChannelAnn(ann, nil); err != nil {
		return r, err
	}
	var encoded bytes.Buffer
	if _, err := lnwire.WriteMessage(&encoded, ann, 0); err != nil {
		return r, err
	}
	decoded, err := lnwire.ReadMessage(bytes.NewReader(encoded.Bytes()), 0)
	if err != nil {
		return r, err
	}
	rebuilt, ok := decoded.(*lnwire.ChannelAnnouncement1)
	if !ok {
		return r, errors.New("wire round trip changed announcement type")
	}
	if err := netann.ValidateChannelAnn(rebuilt, nil); err != nil {
		return r, err
	}
	var reencoded bytes.Buffer
	if _, err := lnwire.WriteMessage(&reencoded, rebuilt, 0); err != nil {
		return r, err
	}
	if !bytes.Equal(encoded.Bytes(), reencoded.Bytes()) {
		return r, errors.New("wire round trip changed signed bytes")
	}
	wireHex := hex.EncodeToString(encoded.Bytes())
	r.Announcement, r.SignatureVerified, r.FundingConstruction = &wireHex, true, "p2wsh"
	// Conservative cohort: any nonzero channel feature bit is reported separately.
	// This avoids misclassifying Taproot/unknown constructions as P2WSH.
	if !ann.Features.IsEmpty() {
		reason := "unsupported_features"
		r.ExclusionReason, r.FundingConstruction = &reason, "unsupported"
	}
	return r, nil
}

func exportDB(dbPath, output string) (retErr error) {
	info, err := os.Lstat(dbPath)
	if err != nil {
		return err
	}
	if !info.Mode().IsRegular() {
		return errors.New("database must be a regular offline copy")
	}
	before, err := hashFile(dbPath)
	if err != nil {
		return err
	}
	db, err := kvdb.GetBoltBackend(&kvdb.BoltBackendConfig{
		DBPath: filepath.Dir(dbPath), DBFileName: filepath.Base(dbPath),
		ReadOnly: true, AutoCompact: false, DBTimeout: time.Second,
	})
	if err != nil {
		return err
	}
	defer db.Close()
	graph, err := graphdb.NewKVStore(db, func(opts *graphdb.StoreOptions) { opts.NoMigration = true })
	if err != nil {
		return err
	}
	hash, height, err := graph.PruneTip(context.Background())
	if err != nil {
		return fmt.Errorf("missing graph prune tip: %w", err)
	}
	result := report{SchemaVersion: schemaVersion, LNDCommit: lndCommit, DatabaseSHA256: before,
		GraphPruneTip: blockRef{height, hash.String()}, Exclusions: make(map[string]int)}
	var rows []record
	err = graph.ForEachChannel(context.Background(), lnwire.GossipVersion1,
		func(edge *models.ChannelEdgeInfo, policy1, policy2 *models.ChannelEdgePolicy) error {
			if edge.ChannelID>>40 > uint64(height) {
				return errors.New("funding height exceeds prune tip")
			}
			r, err := exportRecord(edge)
			if err != nil {
				return fmt.Errorf("invalid graph record: %w", err)
			}
			r.LastUpdate1, r.LastUpdate2 = updateTime(policy1), updateTime(policy2)
			rows = append(rows, r)
			return nil
		}, func() { rows = nil })
	if err != nil {
		return err
	}
	if len(rows) == 0 {
		return errors.New("empty graph")
	}
	sort.Slice(rows, func(i, j int) bool {
		a, _ := strconv.ParseUint(rows[i].SCID, 10, 64)
		b, _ := strconv.ParseUint(rows[j].SCID, 10, 64)
		return a < b
	})
	seenIDs, seenOutputs := make(map[string]bool), make(map[string]bool)
	var data bytes.Buffer
	encoder := json.NewEncoder(&data)
	for _, row := range rows {
		if seenIDs[row.SCID] || seenOutputs[row.FundingOutpoint] {
			return errors.New("duplicate graph record")
		}
		seenIDs[row.SCID], seenOutputs[row.FundingOutpoint] = true, true
		if row.ExclusionReason == nil {
			result.IncludedRecords++
		} else {
			result.Exclusions[*row.ExclusionReason]++
		}
		if err := encoder.Encode(row); err != nil {
			return err
		}
	}
	result.ExportedRecords = len(rows)
	recordHash := sha256.Sum256(data.Bytes())
	result.RecordsSHA256 = hex.EncodeToString(recordHash[:])
	closed, err := closedSCIDs(db)
	if err != nil {
		return fmt.Errorf("closed-channel index: %w", err)
	}
	var closedData bytes.Buffer
	for _, id := range closed {
		if seenIDs[strconv.FormatUint(id, 10)] {
			return errors.New("channel is both exported and marked closed")
		}
		fmt.Fprintln(&closedData, id)
	}
	closedHash := sha256.Sum256(closedData.Bytes())
	result.ClosedSCIDs, result.ClosedSCIDsSHA256 = len(closed), hex.EncodeToString(closedHash[:])
	after, err := hashFile(dbPath)
	if err != nil {
		return err
	}
	if before != after {
		return errors.New("database changed during export")
	}
	if err := os.Mkdir(output, 0700); err != nil {
		return err
	}
	defer func() {
		if retErr != nil {
			_ = os.RemoveAll(output)
		}
	}()
	if err := os.WriteFile(filepath.Join(output, "records.jsonl"), data.Bytes(), 0600); err != nil {
		return err
	}
	if err := os.WriteFile(filepath.Join(output, "closed-scids.txt"), closedData.Bytes(), 0600); err != nil {
		return err
	}
	reportJSON, err := json.MarshalIndent(result, "", "  ")
	if err != nil {
		return err
	}
	return os.WriteFile(filepath.Join(output, "export-report.json"), append(reportJSON, '\n'), 0600)
}

func main() {
	db := flag.String("db", "", "path to stopped LND graph DB copy")
	out := flag.String("output", "", "new output directory (must not exist)")
	flag.Parse()
	if *db == "" || *out == "" {
		flag.Usage()
		os.Exit(2)
	}
	if err := exportDB(*db, *out); err != nil {
		fmt.Fprintln(os.Stderr, "export failed:", err)
		os.Exit(1)
	}
}
