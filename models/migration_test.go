package model

import (
	"testing"

	"github.com/cloudreve/Cloudreve/v3/pkg/conf"
	"github.com/jinzhu/gorm"
	"github.com/stretchr/testify/assert"
)

func TestMigration(t *testing.T) {
	asserts := assert.New(t)
	conf.DatabaseConfig.Type = "sqlite"
	DB, _ = gorm.Open("sqlite", ":memory:")

	asserts.NotPanics(func() {
		migration()
	})
	// 新装的主节点带随机密钥
	node, err := GetNodeByID(1)
	asserts.NoError(err)
	asserts.Len(node.MasterKey, NodeKeyMinLength)
	asserts.Len(node.SlaveKey, NodeKeyMinLength)
	conf.DatabaseConfig.Type = "mysql"
	DB = mockDB
}

func TestEnsureCustomLinkKeys(t *testing.T) {
	asserts := assert.New(t)
	DB, _ = gorm.Open("sqlite", ":memory:")
	DB.DB().SetMaxOpenConns(1)
	defer func() { DB = mockDB }()

	// 旧版数据库：两张表都没有 custom_key 列
	asserts.NoError(DB.Exec("CREATE TABLE shares (id integer primary key autoincrement, source_name varchar(255))").Error)
	asserts.NoError(DB.Exec("CREATE TABLE source_links (id integer primary key autoincrement, file_id integer)").Error)

	// SQLite 的索引名在整个数据库内唯一，两张表的索引不能重名；重复执行不报错
	asserts.NotPanics(ensureCustomLinkKeys)
	asserts.NotPanics(ensureCustomLinkKeys)

	dialect := DB.Dialect()
	asserts.True(dialect.HasColumn("shares", "custom_key"))
	asserts.True(dialect.HasColumn("source_links", "custom_key"))
	asserts.True(dialect.HasIndex("shares", "custom_key"))
	asserts.True(dialect.HasIndex("source_links", "source_link_custom_key"))
}

func TestEnsureNodeKeys(t *testing.T) {
	asserts := assert.New(t)
	DB, _ = gorm.Open("sqlite", ":memory:")
	DB.DB().SetMaxOpenConns(1)
	defer func() { DB = mockDB }()
	asserts.NoError(DB.AutoMigrate(&Node{}).Error)

	good := NewNodeKey()
	asserts.Len(good, NodeKeyMinLength)
	asserts.NotEqual(good, NewNodeKey())
	// 上游默认主节点：两个密钥都为空
	asserts.NoError(DB.Create(&Node{Name: "master", Type: MasterNodeType}).Error)
	// 从机：MasterKey 过短；SlaveKey 必须与从机配置一致，不能动
	asserts.NoError(DB.Create(&Node{Name: "slave", Type: SlaveNodeType, MasterKey: "short", SlaveKey: "slave-secret"}).Error)
	// 密钥都正常的从机保持不变
	asserts.NoError(DB.Create(&Node{Name: "ok", Type: SlaveNodeType, MasterKey: good, SlaveKey: good}).Error)

	asserts.NotPanics(ensureNodeKeys)

	var nodes []Node
	asserts.NoError(DB.Order("id").Find(&nodes).Error)
	asserts.Len(nodes, 3)
	asserts.Len(nodes[0].MasterKey, NodeKeyMinLength)
	asserts.Len(nodes[0].SlaveKey, NodeKeyMinLength)
	asserts.Len(nodes[1].MasterKey, NodeKeyMinLength)
	asserts.Equal("slave-secret", nodes[1].SlaveKey)
	asserts.Equal(good, nodes[2].MasterKey)
	asserts.Equal(good, nodes[2].SlaveKey)

	// 再执行一次不再改动
	first := nodes[0].MasterKey
	asserts.NotPanics(ensureNodeKeys)
	asserts.NoError(DB.Order("id").Find(&nodes).Error)
	asserts.Equal(first, nodes[0].MasterKey)
}
