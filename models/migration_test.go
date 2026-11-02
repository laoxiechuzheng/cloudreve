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
