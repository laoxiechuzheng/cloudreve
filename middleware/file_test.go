package middleware

import (
	"github.com/DATA-DOG/go-sqlmock"
	model "github.com/cloudreve/Cloudreve/v3/models"
	"github.com/cloudreve/Cloudreve/v3/pkg/conf"
	"github.com/cloudreve/Cloudreve/v3/pkg/hashid"
	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/assert"
	"net/http/httptest"
	"testing"
)

func TestValidateSourceLink(t *testing.T) {
	a := assert.New(t)
	rec := httptest.NewRecorder()
	testFunc := ValidateSourceLink()

	// ID 不存在
	{
		c, _ := gin.CreateTestContext(rec)
		testFunc(c)
		a.True(c.IsAborted())
	}

	// SourceLink 不存在
	{
		c, _ := gin.CreateTestContext(rec)
		c.Set("object_id", 1)
		mock.ExpectQuery("SELECT(.+)source_links(.+)").WithArgs(1).WillReturnRows(sqlmock.NewRows([]string{"id"}))
		testFunc(c)
		a.True(c.IsAborted())
		a.NoError(mock.ExpectationsWereMet())
	}

	// 原文件不存在
	{
		c, _ := gin.CreateTestContext(rec)
		c.Set("object_id", 1)
		mock.ExpectQuery("SELECT(.+)source_links(.+)").WithArgs(1).WillReturnRows(sqlmock.NewRows([]string{"id"}).AddRow(1))
		mock.ExpectQuery("SELECT(.+)files(.+)").WithArgs(0).WillReturnRows(sqlmock.NewRows([]string{"id"}))
		testFunc(c)
		a.True(c.IsAborted())
		a.NoError(mock.ExpectationsWereMet())
	}

	// 成功
	{
		c, _ := gin.CreateTestContext(rec)
		c.Set("object_id", 1)
		mock.ExpectQuery("SELECT(.+)source_links(.+)").WithArgs(1).WillReturnRows(sqlmock.NewRows([]string{"id", "file_id"}).AddRow(1, 2))
		mock.ExpectQuery("SELECT(.+)files(.+)").WithArgs(2).WillReturnRows(sqlmock.NewRows([]string{"id"}).AddRow(2))
		mock.ExpectBegin()
		mock.ExpectExec("UPDATE(.+)source_links").WillReturnResult(sqlmock.NewResult(1, 1))
		testFunc(c)
		a.False(c.IsAborted())
		a.NoError(mock.ExpectationsWereMet())
	}

}

func TestValidateSourceLink_Key(t *testing.T) {
	a := assert.New(t)
	rec := httptest.NewRecorder()
	testFunc := ValidateSourceLink()
	conf.SystemConfig.HashIDSalt = ""
	linkHashID := hashid.HashID(1, hashid.SourceLinkID)

	newContext := func(id, name string) *gin.Context {
		c, _ := gin.CreateTestContext(rec)
		c.Params = gin.Params{{Key: "id", Value: id}, {Key: "name", Value: name}}
		return c
	}
	expectDownloaded := func() {
		mock.ExpectBegin()
		mock.ExpectExec("UPDATE(.+)source_links(.+)downloads(.+)").WillReturnResult(sqlmock.NewResult(1, 1))
		mock.ExpectCommit()
	}

	// 自行解析 HashID，文件名一致
	{
		c := newContext(linkHashID, "a.sh")
		mock.ExpectQuery("SELECT(.+)source_links(.+)").WithArgs(1).
			WillReturnRows(sqlmock.NewRows([]string{"id", "file_id"}).AddRow(1, 2))
		mock.ExpectQuery("SELECT(.+)files(.+)").WithArgs(2).
			WillReturnRows(sqlmock.NewRows([]string{"id", "name"}).AddRow(2, "a.sh"))
		expectDownloaded()
		testFunc(c)
		a.False(c.IsAborted())
		link, _ := c.Get("source_link")
		a.EqualValues(1, link.(*model.SourceLink).ID)
		a.NoError(mock.ExpectationsWereMet())
	}

	// HashID 对应的文件名不一致：改按自定义标识查找，也找不到
	{
		c := newContext(linkHashID, "old.sh")
		mock.ExpectQuery("SELECT(.+)source_links(.+)").WithArgs(1).
			WillReturnRows(sqlmock.NewRows([]string{"id", "file_id"}).AddRow(1, 2))
		mock.ExpectQuery("SELECT(.+)files(.+)").WithArgs(2).
			WillReturnRows(sqlmock.NewRows([]string{"id", "name"}).AddRow(2, "a.sh"))
		mock.ExpectQuery("SELECT(.+)source_links(.+)custom_key(.+)").WithArgs(linkHashID).
			WillReturnRows(sqlmock.NewRows([]string{"id"}))
		testFunc(c)
		a.True(c.IsAborted())
		a.NoError(mock.ExpectationsWereMet())
	}

	// HashID 对应外链的文件已删除，该 HashID 被另一个外链用作自定义标识
	{
		c := newContext(linkHashID, "any-name")
		mock.ExpectQuery("SELECT(.+)source_links(.+)").WithArgs(1).
			WillReturnRows(sqlmock.NewRows([]string{"id", "file_id"}).AddRow(1, 2))
		mock.ExpectQuery("SELECT(.+)files(.+)").WithArgs(2).
			WillReturnRows(sqlmock.NewRows([]string{"id"}))
		mock.ExpectQuery("SELECT(.+)source_links(.+)custom_key(.+)").WithArgs(linkHashID).
			WillReturnRows(sqlmock.NewRows([]string{"id", "file_id", "custom_key"}).AddRow(3, 4, linkHashID))
		mock.ExpectQuery("SELECT(.+)files(.+)").WithArgs(4).
			WillReturnRows(sqlmock.NewRows([]string{"id", "name"}).AddRow(4, "new.sh"))
		expectDownloaded()
		testFunc(c)
		a.False(c.IsAborted())
		link, _ := c.Get("source_link")
		a.EqualValues(3, link.(*model.SourceLink).ID)
		a.NoError(mock.ExpectationsWereMet())
	}

	// 自定义标识不检查文件名
	{
		c := newContext("98k-win", "deploy-old.sh")
		mock.ExpectQuery("SELECT(.+)source_links(.+)custom_key(.+)").WithArgs("98k-win").
			WillReturnRows(sqlmock.NewRows([]string{"id", "file_id", "custom_key"}).AddRow(3, 4, "98k-win"))
		mock.ExpectQuery("SELECT(.+)files(.+)").WithArgs(4).
			WillReturnRows(sqlmock.NewRows([]string{"id", "name"}).AddRow(4, "deploy-new.sh"))
		expectDownloaded()
		testFunc(c)
		a.False(c.IsAborted())
		link, _ := c.Get("source_link")
		a.Equal("deploy-new.sh", link.(*model.SourceLink).File.Name)
		a.NoError(mock.ExpectationsWereMet())
	}

	// 自定义标识对应的文件已删除
	{
		c := newContext("98k-win", "deploy.sh")
		mock.ExpectQuery("SELECT(.+)source_links(.+)custom_key(.+)").WithArgs("98k-win").
			WillReturnRows(sqlmock.NewRows([]string{"id", "file_id", "custom_key"}).AddRow(3, 4, "98k-win"))
		mock.ExpectQuery("SELECT(.+)files(.+)").WithArgs(4).
			WillReturnRows(sqlmock.NewRows([]string{"id"}))
		testFunc(c)
		a.True(c.IsAborted())
		a.NoError(mock.ExpectationsWereMet())
	}

	// 既不是 HashID 也不是合法的自定义标识，不查询数据库
	{
		c := newContext("a b", "deploy.sh")
		testFunc(c)
		a.True(c.IsAborted())
		a.NoError(mock.ExpectationsWereMet())
	}
}
